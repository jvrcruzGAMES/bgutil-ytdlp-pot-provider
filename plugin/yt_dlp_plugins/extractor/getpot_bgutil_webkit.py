from __future__ import annotations
import json

from yt_dlp.extractor.youtube.pot.provider import (
    PoTokenContext,
    PoTokenProvider,
    PoTokenProviderError,
    PoTokenRequest,
    PoTokenResponse,
    register_preference,
    register_provider,
)
from yt_dlp.extractor.youtube.pot.utils import get_webpo_content_binding, WEBPO_CLIENTS

try:
    from yt_dlp_plugins.extractor.webkit_jsi import AppleWebKitMixin
    HAS_WEBKIT = True
except ImportError:
    HAS_WEBKIT = False


# Self-contained browser-compatible JS solver logic
JAVASCRIPT_SOLVER = r"""
async function getPoToken(contentBinding) {
    // 1. Fetch challenge from InnerTube API /att/get
    const origin = window.location.origin && window.location.origin.includes("youtube.com")
        ? window.location.origin
        : "https://www.youtube.com";
    const attGetResponse = await fetch(origin + "/youtubei/v1/att/get?prettyPrint=false", {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
        },
        body: JSON.stringify({
            context: {
                client: {
                    clientName: "WEB",
                    clientVersion: "2.20260227.01.00",
                },
            },
            engagementType: "ENGAGEMENT_TYPE_UNBOUND",
        }),
    });
    if (!attGetResponse.ok) {
        throw new Error("Failed to fetch attestation challenge: " + attGetResponse.status);
    }
    const attestation = await attGetResponse.json();
    const challenge = attestation.bgChallenge;
    if (!challenge) {
        throw new Error("No challenge found in attestation response");
    }

    const { program, globalName } = challenge;
    const interpreterUrl = challenge.interpreterUrl.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
    
    // 2. Fetch the interpreter JS VM
    const interpreterResponse = await fetch("https:" + interpreterUrl);
    if (!interpreterResponse.ok) {
        throw new Error("Failed to fetch interpreter JS: " + interpreterResponse.status);
    }
    const interpreterJS = await interpreterResponse.text();

    // 3. Evaluate the interpreter JS in global scope
    const evalScript = document.createElement("script");
    evalScript.text = interpreterJS;
    document.head.appendChild(evalScript);
    document.head.removeChild(evalScript);

    const vm = window[globalName];
    if (!vm || !vm.a) {
        throw new Error("BotGuard VM not initialized properly");
    }

    // 4. Load the VM and wait for the async callback
    const vmFunctionsPromise = new Promise((resolve) => {
        vm.a(program, (asyncSnapshotFunction, shutdownFunction, passEventFunction, checkCameraFunction) => {
            resolve({ asyncSnapshotFunction, shutdownFunction });
        }, true, null, () => {}, [[], []]);
    });

    const { asyncSnapshotFunction, shutdownFunction } = await vmFunctionsPromise;

    // 5. Run snapshot to get the botguardResponse and webPoSignalOutput
    const webPoSignalOutput = [];
    const botguardResponsePromise = new Promise((resolve) => {
        asyncSnapshotFunction((response) => resolve(response), [
            undefined, // contentBinding
            undefined, // signedTimestamp
            webPoSignalOutput,
            undefined // skipPrivacyBuffer
        ]);
    });

    const botguardResponse = await botguardResponsePromise;

    // 6. Send snapshot to GenerateIT
    const REQUEST_KEY = "O43z0dpjhgX20SCx4KAo";
    const generateItResponse = await fetch("https://jnn-pa.googleapis.com/$rpc/google.internal.waa.v1.Waa/GenerateIT", {
        method: "POST",
        headers: {
            "Content-Type": "application/json+protobuf",
            "x-goog-api-key": "AIzaSyDyT5W0Jh49F30Pqqtyfdf7pDLFKLJoAnw",
            "x-user-agent": "grpc-web-javascript/0.1"
        },
        body: JSON.stringify([REQUEST_KEY, botguardResponse])
    });

    if (!generateItResponse.ok) {
        throw new Error("Failed to call GenerateIT: " + generateItResponse.status);
    }
    const integrityTokenJson = await generateItResponse.json();
    const integrityToken = integrityTokenJson[0];
    if (!integrityToken) {
        throw new Error("Empty integrity token received: " + JSON.stringify(integrityTokenJson));
    }

    // 7. Decode integrityToken from base64
    const base64urlCharRegex = /[-_.]/g;
    const base64urlToBase64Map = { '-': '+', '_': '/', '.': '=' };
    let base64Mod = integrityToken;
    if (base64urlCharRegex.test(base64Mod)) {
        base64Mod = base64Mod.replace(base64urlCharRegex, (match) => base64urlToBase64Map[match]);
    }
    const decodedIntegrityStr = atob(base64Mod);
    const decodedIntegrityU8 = new Uint8Array([...decodedIntegrityStr].map((char) => char.charCodeAt(0)));

    // 8. Mint the PO Token using getMinter and contentBinding
    const getMinter = webPoSignalOutput[0];
    if (!getMinter) {
        throw new Error("webPoSignalOutput does not contain getMinter function");
    }
    const mintCallback = await getMinter(decodedIntegrityU8);
    if (!mintCallback || typeof mintCallback !== 'function') {
        throw new Error("getMinter did not return a valid function");
    }

    const mintedTokenU8 = await mintCallback(new TextEncoder().encode(contentBinding));
    if (!mintedTokenU8 || !(mintedTokenU8 instanceof Uint8Array)) {
        throw new Error("mintCallback did not return a valid Uint8Array");
    }

    // Convert Uint8Array to base64url
    const base64Result = btoa(String.fromCharCode(...mintedTokenU8))
        .replace(/\+/g, '-')
        .replace(/\//g, '_');

    // Shutdown the VM
    if (shutdownFunction) {
        try { shutdownFunction(); } catch(e) {}
    }

    return base64Result;
}
"""


if HAS_WEBKIT:
    @register_provider
    class BgUtilWebKitPTP(AppleWebKitMixin, PoTokenProvider):
        PROVIDER_NAME = 'bgutil:webkit'
        _SUPPORTED_CLIENTS = WEBPO_CLIENTS
        _SUPPORTED_CONTEXTS = (
            PoTokenContext.GVS,
            PoTokenContext.PLAYER,
            PoTokenContext.SUBS,
        )

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._try_init_factory()

        def _real_request_pot(self, request: PoTokenRequest) -> PoTokenResponse:
            self.logger.info(
                f'Generating a {request.context.value} PO Token for '
                f'{request.internal_client_name} client via bgutil:webkit (Apple WebKit JSI)'
            )
            
            # 1. Get the lazily initialized webview instance
            webview = self._get_webview_lazy()
            
            # 2. Establish cookies and origin context by navigating to youtube.com
            self.logger.info('Navigating hidden WebKit webview to youtube.com to establish origin context...')
            webview.navigate_to('https://www.youtube.com/', '__REMOTE__')
            
            # 3. Retrieve the content binding parameter
            content_binding = get_webpo_content_binding(request)[0]
            self.logger.info(f'Content binding used: {content_binding}')
            
            # 4. Wrap JS solver function inside the execution script
            js_code = f"""
            {JAVASCRIPT_SOLVER}
            return await getPoToken({json.dumps(content_binding)});
            """
            
            # 5. Run the challenge solver inside WebKit and return the result
            try:
                self.logger.info('Executing client-side BotGuard challenge solver inside WebKit...')
                po_token = webview.execute_js(js_code)
                if not po_token:
                    raise PoTokenProviderError('WebKit returned an empty PO Token')
                self.logger.info(f'Successfully generated PO Token via WebKit: {po_token[:15]}... ({len(po_token)} chars)')
                return PoTokenResponse(po_token=po_token)
            except Exception as e:
                self.logger.error(f'WebKit POT generation failed: {e!r}')
                raise PoTokenProviderError(f'Failed to generate POT in WebKit (caused by {e!r})') from e


    @register_preference(BgUtilWebKitPTP)
    def bgutil_webkit_getpot_preference(provider, request):
        # Set preference higher than localhost server (130) or external process script (20)
        return 140


    __all__ = ['BgUtilWebKitPTP', 'bgutil_webkit_getpot_preference']
else:
    __all__ = []
