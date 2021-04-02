"""policy API Views"""
from django.core.cache import cache
from drf_yasg.utils import no_body, swagger_auto_schema
from guardian.shortcuts import get_objects_for_user
from rest_framework import mixins
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.serializers import ModelSerializer, SerializerMethodField
from rest_framework.viewsets import GenericViewSet
from structlog.stdlib import get_logger

from authentik.api.decorators import permission_required
from authentik.core.api.applications import user_app_cache_key
from authentik.core.api.utils import (
    CacheSerializer,
    MetaNameSerializer,
    TypeCreateSerializer,
)
from authentik.lib.templatetags.authentik_utils import verbose_name
from authentik.lib.utils.reflection import all_subclasses
from authentik.policies.api.exec import PolicyTestResultSerializer, PolicyTestSerializer
from authentik.policies.models import Policy, PolicyBinding
from authentik.policies.process import PolicyProcess
from authentik.policies.types import PolicyRequest

LOGGER = get_logger()


class PolicySerializer(ModelSerializer, MetaNameSerializer):
    """Policy Serializer"""

    _resolve_inheritance: bool

    object_type = SerializerMethodField()
    bound_to = SerializerMethodField()

    def __init__(self, *args, resolve_inheritance: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolve_inheritance = resolve_inheritance

    def get_object_type(self, obj: Policy) -> str:
        """Get object type so that we know which API Endpoint to use to get the full object"""
        return obj._meta.object_name.lower().replace("policy", "")

    def get_bound_to(self, obj: Policy) -> int:
        """Return objects policy is bound to"""
        if not obj.bindings.exists() and not obj.promptstage_set.exists():
            return 0
        return obj.bindings.count()

    def to_representation(self, instance: Policy):
        # pyright: reportGeneralTypeIssues=false
        if instance.__class__ == Policy or not self._resolve_inheritance:
            return super().to_representation(instance)
        return dict(
            instance.serializer(instance=instance, resolve_inheritance=False).data
        )

    class Meta:

        model = Policy
        fields = [
            "pk",
            "name",
            "execution_logging",
            "object_type",
            "verbose_name",
            "verbose_name_plural",
            "bound_to",
        ]
        depth = 3


class PolicyViewSet(
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    mixins.ListModelMixin,
    GenericViewSet,
):
    """Policy Viewset"""

    queryset = Policy.objects.all()
    serializer_class = PolicySerializer
    filterset_fields = {
        "bindings": ["isnull"],
        "promptstage": ["isnull"],
    }
    search_fields = ["name"]

    def get_queryset(self):
        return Policy.objects.select_subclasses().prefetch_related(
            "bindings", "promptstage_set"
        )

    @swagger_auto_schema(responses={200: TypeCreateSerializer(many=True)})
    @action(detail=False, pagination_class=None, filter_backends=[])
    def types(self, request: Request) -> Response:
        """Get all creatable policy types"""
        data = []
        for subclass in all_subclasses(self.queryset.model):
            data.append(
                {
                    "name": verbose_name(subclass),
                    "description": subclass.__doc__,
                    "link": subclass().component,
                }
            )
        return Response(TypeCreateSerializer(data, many=True).data)

    @permission_required("authentik_policies.view_policy_cache")
    @swagger_auto_schema(responses={200: CacheSerializer(many=False)})
    @action(detail=False, pagination_class=None, filter_backends=[])
    def cache_info(self, request: Request) -> Response:
        """Info about cached policies"""
        return Response(data={"count": len(cache.keys("policy_*"))})

    @permission_required("authentik_policies.clear_policy_cache")
    @swagger_auto_schema(
        request_body=no_body,
        responses={204: "Successfully cleared cache", 400: "Bad request"},
    )
    @action(detail=False, methods=["POST"])
    def cache_clear(self, request: Request) -> Response:
        """Clear policy cache"""
        keys = cache.keys("policy_*")
        cache.delete_many(keys)
        LOGGER.debug("Cleared Policy cache", keys=len(keys))
        # Also delete user application cache
        keys = cache.keys(user_app_cache_key("*"))
        cache.delete_many(keys)
        return Response(status=204)

    @permission_required("authentik_policies.view_policy")
    @swagger_auto_schema(
        request_body=PolicyTestSerializer(),
        responses={200: PolicyTestResultSerializer()},
    )
    @action(detail=True, pagination_class=None, filter_backends=[], methods=["POST"])
    # pylint: disable=unused-argument, invalid-name
    def test(self, request: Request, pk: str) -> Response:
        """Test policy"""
        policy = self.get_object()
        test_params = PolicyTestSerializer(data=request.data)
        if not test_params.is_valid():
            return Response(test_params.errors, status=400)

        # User permission check, only allow policy testing for users that are readable
        users = get_objects_for_user(request.user, "authentik_core.view_user").filter(
            pk=test_params.validated_data["user"].pk
        )
        if not users.exists():
            raise PermissionDenied()

        p_request = PolicyRequest(users.first())
        p_request.debug = True
        p_request.set_http_request(self.request)
        p_request.context = test_params.validated_data.get("context", {})

        proc = PolicyProcess(PolicyBinding(policy=policy), p_request, None)
        result = proc.execute()
        response = PolicyTestResultSerializer(result)
        return Response(response.data)
