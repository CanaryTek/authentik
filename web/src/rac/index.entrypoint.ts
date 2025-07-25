import "#elements/LoadingOverlay";

import AKGlobal from "#common/styles/authentik.css";

import { Interface } from "#elements/Interface";
import { WithBrandConfig } from "#elements/mixins/branding";

import Guacamole from "guacamole-common-js";

import { msg, str } from "@lit/localize";
import { css, CSSResult, html, nothing, TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";

import PFContent from "@patternfly/patternfly/components/Content/content.css";
import PFPage from "@patternfly/patternfly/components/Page/page.css";
import PFBase from "@patternfly/patternfly/patternfly-base.css";

enum GuacClientState {
    IDLE = 0,
    CONNECTING = 1,
    WAITING = 2,
    CONNECTED = 3,
    DISCONNECTING = 4,
    DISCONNECTED = 5,
}

export function GuacStateToString(state: GuacClientState): string {
    switch (state) {
        case GuacClientState.IDLE:
            return msg("Idle");
        case GuacClientState.CONNECTING:
            return msg("Connecting");
        case GuacClientState.WAITING:
            return msg("Waiting");
        case GuacClientState.CONNECTED:
            return msg("Connected");
        case GuacClientState.DISCONNECTING:
            return msg("Disconnecting");
        case GuacClientState.DISCONNECTED:
            return msg("Disconnected");
    }
}

const AUDIO_INPUT_MIMETYPE = "audio/L16;rate=44100,channels=2";
const RECONNECT_ATTEMPTS_INITIAL = 5;
const RECONNECT_ATTEMPTS = 5;

@customElement("ak-rac")
export class RacInterface extends WithBrandConfig(Interface) {
    static styles: CSSResult[] = [
        PFBase,
        PFPage,
        PFContent,
        AKGlobal,
        css`
            :host {
                cursor: none;
            }
            canvas {
                z-index: unset !important;
            }
            .container {
                overflow: hidden;
                height: 100vh;
                background-color: black;
                display: flex;
                justify-content: center;
                align-items: center;
            }
            ak-loading-overlay {
                z-index: 5;
            }
        `,
    ];

    client?: Guacamole.Client;
    tunnel?: Guacamole.Tunnel;

    @state()
    container?: HTMLElement;

    @state()
    clientState?: GuacClientState;

    @state()
    clientStatus?: Guacamole.Status;

    @state()
    reconnectingMessage = "";

    @property()
    token?: string;

    @property()
    endpointName?: string;

    @state()
    clipboardWatcherTimer = 0;

    _previousClipboardValue: unknown;

    // Set to `true` if we've successfully connected once
    hasConnected = false;
    // Keep track of current connection attempt
    connectionAttempt = 0;

    static domSize(): { width: number; height: number } {
        const size = document.body.getBoundingClientRect();
        return {
            width: size.width,
            height: size.height,
        };
    }

    constructor() {
        super();
        this.initKeyboard();
        this.checkClipboard();
        this.clipboardWatcherTimer = setInterval(
            this.checkClipboard.bind(this),
            500,
        ) as unknown as number;
    }

    connectedCallback(): void {
        super.connectedCallback();
        window.addEventListener(
            "focus",
            () => {
                this.checkClipboard();
            },
            {
                capture: false,
            },
        );
        window.addEventListener("resize", () => {
            this.client?.sendSize(
                Math.floor(RacInterface.domSize().width),
                Math.floor(RacInterface.domSize().height),
            );
        });
    }

    disconnectedCallback(): void {
        super.disconnectedCallback();
        clearInterval(this.clipboardWatcherTimer);
    }

    async firstUpdated(): Promise<void> {
        this.updateTitle();
        const wsUrl = `${window.location.protocol.replace("http", "ws")}//${
            window.location.host
        }/ws/rac/${this.token}/`;
        this.tunnel = new Guacamole.WebSocketTunnel(wsUrl);
        this.tunnel.receiveTimeout = 10 * 1000; // 10 seconds
        this.tunnel.onerror = (status) => {
            console.debug("authentik/rac: tunnel error: ", status);
            this.reconnect();
        };
        this.client = new Guacamole.Client(this.tunnel);
        this.client.onerror = (err) => {
            this.clientStatus = err;
            console.debug("authentik/rac: error: ", err);
            this.reconnect();
        };
        this.client.onstatechange = (state) => {
            this.clientState = state;
            if (state === GuacClientState.CONNECTED) {
                this.onConnected();
            }
        };
        this.client.onclipboard = (stream, mimetype) => {
            // If the received data is text, read it as a simple string
            if (/^text\//.exec(mimetype)) {
                const reader = new Guacamole.StringReader(stream);
                let data = "";
                reader.ontext = (text) => {
                    data += text;
                };
                reader.onend = () => {
                    this._previousClipboardValue = data;
                    navigator.clipboard.writeText(data);
                };
            } else {
                const reader = new Guacamole.BlobReader(stream, mimetype);
                reader.onend = () => {
                    const blob = reader.getBlob();
                    navigator.clipboard.write([
                        new ClipboardItem({
                            [blob.type]: blob,
                        }),
                    ]);
                };
            }
            console.debug("authentik/rac: updated clipboard from remote");
        };
        const params = new URLSearchParams();
        params.set("screen_width", Math.floor(RacInterface.domSize().width).toString());
        params.set("screen_height", Math.floor(RacInterface.domSize().height).toString());
        // https://github.com/goauthentik/authentik/pull/11757
        // there are DPI issues when using SSH on HiDPi screens
        // but if we're not setting DPI at all the resolution is not respected at all
        params.set("screen_dpi", "96");
        this.client.connect(params.toString());
    }

    reconnect(): void {
        this.clientState = undefined;
        this.connectionAttempt += 1;
        if (!this.hasConnected) {
            // Check connection attempts if we haven't had a successful connection
            if (this.connectionAttempt >= RECONNECT_ATTEMPTS_INITIAL) {
                this.hasConnected = true;
                this.reconnectingMessage = msg(
                    str`Connection failed after ${this.connectionAttempt} attempts.`,
                );
                return;
            }
        } else if (this.connectionAttempt >= RECONNECT_ATTEMPTS) {
            this.reconnectingMessage = msg(
                str`Connection failed after ${this.connectionAttempt} attempts.`,
            );
            return;
        }
        const delay = 500 * this.connectionAttempt;
        this.reconnectingMessage = msg(
            str`Re-connecting in ${Math.max(1, delay / 1000)} second(s).`,
        );
        setTimeout(() => {
            this.firstUpdated();
        }, delay);
    }

    updateTitle(): void {
        let title = this.brandingTitle;

        if (this.endpointName) {
            title = `${this.endpointName} - ${title}`;
        }

        document.title = `${title}`;
    }

    onConnected(): void {
        console.debug("authentik/rac: connected");
        if (!this.client) {
            return;
        }
        this.hasConnected = true;
        this.clientStatus = undefined;
        this.container = this.client.getDisplay().getElement();
        this.initMouse(this.container);
        this.client?.sendSize(
            Math.floor(RacInterface.domSize().width),
            Math.floor(RacInterface.domSize().height),
        );
    }

    initMouse(container: HTMLElement): void {
        const mouse = new Guacamole.Mouse(container);
        const handler = (mouseState: Guacamole.Mouse.State, scaleMouse = false) => {
            if (!this.client) return;

            if (scaleMouse) {
                mouseState.y = mouseState.y / this.client.getDisplay().getScale();
                mouseState.x = mouseState.x / this.client.getDisplay().getScale();
            }

            this.client.sendMouseState(mouseState);
        };
        // @ts-ignore
        mouse.onEach(["mouseup", "mousedown"], (ev: Guacamole.Mouse.Event) => {
            this.container?.focus();
            handler(ev.state);
        });
        // @ts-ignore
        mouse.on("mousemove", (ev: Guacamole.Mouse.Event) => {
            handler(ev.state, true);
        });
    }

    initAudioInput(): void {
        const stream = this.client?.createAudioStream(AUDIO_INPUT_MIMETYPE);
        if (!stream) return;
        // Guacamole.AudioPlayer
        const recorder = Guacamole.AudioRecorder.getInstance(stream, AUDIO_INPUT_MIMETYPE);
        // If creation of the AudioRecorder failed, simply end the stream
        if (!recorder) {
            stream.sendEnd();
            return;
        }
        // Otherwise, ensure that another audio stream is created after this
        // audio stream is closed
        recorder.onclose = this.initAudioInput.bind(this);
    }

    initKeyboard(): void {
        const keyboard = new Guacamole.Keyboard(document);
        keyboard.onkeydown = (keysym) => {
            this.client?.sendKeyEvent(1, keysym);
        };
        keyboard.onkeyup = (keysym) => {
            this.client?.sendKeyEvent(0, keysym);
        };
    }

    async checkClipboard(): Promise<void> {
        try {
            if (!this._previousClipboardValue) {
                this._previousClipboardValue = await navigator.clipboard.readText();
                return;
            }
            const newValue = await navigator.clipboard.readText();
            if (newValue !== this._previousClipboardValue) {
                console.debug(`authentik/rac: new clipboard value: ${newValue}`);
                this._previousClipboardValue = newValue;
                this.writeClipboard(newValue);
            }
        } catch (ex) {
            // The error is most likely caused by the document not being in focus
            // in which case we can ignore it and just retry
            if (ex instanceof DOMException) {
                return;
            }
            console.warn("authentik/rac: error reading clipboard", ex);
        }
    }

    private writeClipboard(value: string) {
        if (!this.client) {
            return;
        }
        const stream = this.client.createClipboardStream("text/plain");
        const writer = new Guacamole.StringWriter(stream);
        writer.sendText(value);
        writer.sendEnd();
        console.debug("authentik/rac: Sent clipboard");
    }

    renderOverlay() {
        if (!this.clientState || this.clientState === GuacClientState.CONNECTED) {
            return nothing;
        }
        let message = html`${GuacStateToString(this.clientState)}`;
        if (this.clientState === GuacClientState.WAITING) {
            message = html`${msg("Connecting...")}`;
        }
        if (this.hasConnected) {
            message = html`${this.reconnectingMessage}`;
        }
        if (this.clientStatus?.message) {
            message = html`${message}<br />${this.clientStatus.message}`;
        }
        const isLoading = [
            GuacClientState.CONNECTING,
            GuacClientState.DISCONNECTING,
            GuacClientState.WAITING,
        ].includes(this.clientState);
        return html`
            <ak-loading-overlay ?no-spinner=${!isLoading} icon="fa fa-times">
                <span>${message}</span>
            </ak-loading-overlay>
        `;
    }

    render(): TemplateResult {
        return html`
            ${this.renderOverlay()}
            <div class="container">${this.container}</div>
        `;
    }
}

declare global {
    interface HTMLElementTagNameMap {
        "ak-rac": RacInterface;
    }
}
