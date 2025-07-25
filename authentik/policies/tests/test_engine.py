"""policy engine tests"""

from django.core.cache import cache
from django.db import connections
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from authentik.core.models import Group
from authentik.core.tests.utils import create_test_user
from authentik.lib.generators import generate_id
from authentik.policies.dummy.models import DummyPolicy
from authentik.policies.engine import PolicyEngine
from authentik.policies.exceptions import PolicyEngineException
from authentik.policies.expression.models import ExpressionPolicy
from authentik.policies.models import Policy, PolicyBinding, PolicyBindingModel, PolicyEngineMode
from authentik.policies.tests.test_process import clear_policy_cache
from authentik.policies.types import CACHE_PREFIX


class TestPolicyEngine(TestCase):
    """PolicyEngine tests"""

    def setUp(self):
        clear_policy_cache()
        self.user = create_test_user()
        self.policy_false = DummyPolicy.objects.create(
            name=generate_id(), result=False, wait_min=0, wait_max=1
        )
        self.policy_true = DummyPolicy.objects.create(
            name=generate_id(), result=True, wait_min=0, wait_max=1
        )
        self.policy_wrong_type = Policy.objects.create(name=generate_id())
        self.policy_raises = ExpressionPolicy.objects.create(
            name=generate_id(), expression="{{ 0/0 }}"
        )

    def test_engine_empty(self):
        """Ensure empty policy list passes"""
        pbm = PolicyBindingModel.objects.create()
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, True)
        self.assertEqual(result.messages, ())

    def test_engine_simple(self):
        """Ensure simplest use-case"""
        pbm = PolicyBindingModel.objects.create()
        PolicyBinding.objects.create(target=pbm, policy=self.policy_true, order=0)
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, True)
        self.assertEqual(result.messages, ("dummy",))

    def test_engine_mode_all(self):
        """Ensure all policies passes with AND mode (false and true -> false)"""
        pbm = PolicyBindingModel.objects.create(policy_engine_mode=PolicyEngineMode.MODE_ALL)
        PolicyBinding.objects.create(target=pbm, policy=self.policy_false, order=0)
        PolicyBinding.objects.create(target=pbm, policy=self.policy_true, order=1)
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, False)
        self.assertEqual(
            result.messages,
            (
                "dummy",
                "dummy",
            ),
        )

    def test_engine_mode_any(self):
        """Ensure all policies passes with OR mode (false and true -> true)"""
        pbm = PolicyBindingModel.objects.create(policy_engine_mode=PolicyEngineMode.MODE_ANY)
        PolicyBinding.objects.create(target=pbm, policy=self.policy_false, order=0)
        PolicyBinding.objects.create(target=pbm, policy=self.policy_true, order=1)
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, True)
        self.assertEqual(
            result.messages,
            (
                "dummy",
                "dummy",
            ),
        )

    def test_engine_negate(self):
        """Test negate flag"""
        pbm = PolicyBindingModel.objects.create()
        PolicyBinding.objects.create(target=pbm, policy=self.policy_true, negate=True, order=0)
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, False)
        self.assertEqual(result.messages, ("dummy",))

    def test_engine_policy_error(self):
        """Test policy raising an error flag"""
        pbm = PolicyBindingModel.objects.create()
        PolicyBinding.objects.create(target=pbm, policy=self.policy_raises, order=0)
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, False)
        self.assertEqual(result.messages, ("division by zero",))

    def test_engine_policy_error_failure(self):
        """Test policy raising an error flag"""
        pbm = PolicyBindingModel.objects.create()
        PolicyBinding.objects.create(
            target=pbm, policy=self.policy_raises, order=0, failure_result=True
        )
        engine = PolicyEngine(pbm, self.user)
        result = engine.build().result
        self.assertEqual(result.passing, True)
        self.assertEqual(result.messages, ("division by zero",))

    def test_engine_policy_type(self):
        """Test invalid policy type"""
        pbm = PolicyBindingModel.objects.create()
        PolicyBinding.objects.create(target=pbm, policy=self.policy_wrong_type, order=0)
        with self.assertRaises(PolicyEngineException):
            engine = PolicyEngine(pbm, self.user)
            engine.build()

    def test_engine_cache(self):
        """Ensure empty policy list passes"""
        pbm = PolicyBindingModel.objects.create()
        binding = PolicyBinding.objects.create(target=pbm, policy=self.policy_false, order=0)
        engine = PolicyEngine(pbm, self.user)
        self.assertEqual(len(cache.keys(f"{CACHE_PREFIX}{binding.policy_binding_uuid.hex}*")), 0)
        self.assertEqual(engine.build().passing, False)
        self.assertEqual(len(cache.keys(f"{CACHE_PREFIX}{binding.policy_binding_uuid.hex}*")), 1)
        self.assertEqual(engine.build().passing, False)
        self.assertEqual(len(cache.keys(f"{CACHE_PREFIX}{binding.policy_binding_uuid.hex}*")), 1)

    def test_engine_static_bindings(self):
        """Test static bindings"""
        group_a = Group.objects.create(name=generate_id())
        group_b = Group.objects.create(name=generate_id())
        group_b.users.add(self.user)
        user = create_test_user()

        for case in [
            {
                "message": "Group, not member",
                "binding_args": {"group": group_a},
                "passing": False,
            },
            {
                "message": "Group, member",
                "binding_args": {"group": group_b},
                "passing": True,
            },
            {
                "message": "User, other",
                "binding_args": {"user": user},
                "passing": False,
            },
            {
                "message": "User, same",
                "binding_args": {"user": self.user},
                "passing": True,
            },
        ]:
            with self.subTest():
                pbm = PolicyBindingModel.objects.create()
                for x in range(1000):
                    PolicyBinding.objects.create(target=pbm, order=x, **case["binding_args"])
                engine = PolicyEngine(pbm, self.user)
                engine.use_cache = False
                with CaptureQueriesContext(connections["default"]) as ctx:
                    engine.build()
                self.assertLess(ctx.final_queries, 1000)
                self.assertEqual(engine.result.passing, case["passing"])

    def test_engine_group_complex(self):
        """Test more complex group setups"""
        group_a = Group.objects.create(name=generate_id())
        group_b = Group.objects.create(name=generate_id(), parent=group_a)
        user = create_test_user()
        group_b.users.add(user)
        pbm = PolicyBindingModel.objects.create()
        PolicyBinding.objects.create(target=pbm, order=0, group=group_a)
        engine = PolicyEngine(pbm, user)
        engine.use_cache = False
        with CaptureQueriesContext(connections["default"]) as ctx:
            engine.build()
        self.assertLess(ctx.final_queries, 1000)
        self.assertTrue(engine.result.passing)
