const QR_CODE_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js';
const DEFAULT_WIREGUARD_CONFIG = `[Interface]
PrivateKey = REPLACE_WITH_CLIENT_PRIVATE_KEY
Address = 10.8.0.2/32
DNS = 1.1.1.1

[Peer]
PublicKey = REPLACE_WITH_SERVER_PUBLIC_KEY
Endpoint = vpn.example.com:51820
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25`;

const providerTabs = document.querySelectorAll('.provider-tab');
const scalewaySection = document.getElementById('scaleway-section');
const digitaloceanSection = document.getElementById('digitalocean-section');
const scalewayZoneSelect = document.getElementById('scaleway-zone');
const scalewayDeployBtn = document.getElementById('scaleway-deploy');
const scalewayDestroyBtn = document.getElementById('scaleway-destroy');
const doRegionSelect = document.getElementById('do-region');
const doDeployBtn = document.getElementById('do-deploy');
const doDestroyBtn = document.getElementById('do-destroy');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const ipAddress = document.getElementById('ip-address');
const statusCard = document.getElementById('status-card');
const qrContainer = document.getElementById('qrcode');

let deployTimer = null;

function loadExternalScript(src) {
    return new Promise((resolve, reject) => {
        const existingScript = document.querySelector(`script[src="${src}"]`);

        if (existingScript) {
            if (window.QRCode) {
                resolve();
                return;
            }

            existingScript.addEventListener('load', () => resolve(), { once: true });
            existingScript.addEventListener('error', () => reject(new Error(`Failed to load ${src}`)), { once: true });
            return;
        }

        const script = document.createElement('script');
        script.src = src;
        script.async = true;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error(`Failed to load ${src}`));
        document.head.appendChild(script);
    });
}

function setActiveProvider(provider) {
    providerTabs.forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.provider === provider);
    });

    scalewaySection.classList.toggle('hidden-section', provider !== 'scaleway');
    digitaloceanSection.classList.toggle('hidden-section', provider !== 'digitalocean');
}

function updateDeployButtons() {
    scalewayDeployBtn.disabled = !scalewayZoneSelect.value;
    doDeployBtn.disabled = !doRegionSelect.value;
}

function resetStatus() {
    if (deployTimer) {
        clearTimeout(deployTimer);
        deployTimer = null;
    }

    statusCard.classList.remove('active');
    statusDot.className = 'status-indicator';
    statusText.textContent = 'Disconnected';
    ipAddress.textContent = '—';
    scalewayDestroyBtn.disabled = true;
    doDestroyBtn.disabled = true;
}

function simulateDeploy(provider) {
    statusCard.classList.add('active');
    statusDot.className = 'status-indicator connecting';
    statusText.textContent = 'Connecting...';
    ipAddress.textContent = '—';

    const destroyBtn = provider === 'scaleway' ? scalewayDestroyBtn : doDestroyBtn;
    destroyBtn.disabled = false;

    if (deployTimer) {
        clearTimeout(deployTimer);
    }

    deployTimer = window.setTimeout(() => {
        statusDot.className = 'status-indicator connected';
        statusText.textContent = 'Connected';
        ipAddress.textContent = '203.0.113.42';
        deployTimer = null;
    }, 2000);
}

function simulateDestroy() {
    resetStatus();
    scalewayZoneSelect.value = '';
    doRegionSelect.value = '';
    updateDeployButtons();
}

function invokeNativeDeploy(provider) {
    const region = provider === 'scaleway' ? scalewayZoneSelect.value : doRegionSelect.value;
    const nativeApi = window.pywebview?.api;

    if (nativeApi?.deploy) {
        return Boolean(nativeApi.deploy(provider, region));
    }

    return false;
}

function invokeNativeDestroy(provider) {
    const nativeApi = window.pywebview?.api;

    if (nativeApi?.destroy) {
        return Boolean(nativeApi.destroy(provider));
    }

    return false;
}

function renderQrCode(configText) {
    if (!qrContainer) {
        return;
    }

    qrContainer.innerHTML = '';

    if (!window.QRCode) {
        qrContainer.textContent = 'QR code unavailable';
        return;
    }

    new QRCode(qrContainer, {
        text: configText,
        width: 152,
        height: 152,
        correctLevel: QRCode.CorrectLevel.M,
    });
}

function initialize() {
    providerTabs.forEach((tab) => {
        tab.addEventListener('click', () => setActiveProvider(tab.dataset.provider));
    });

    scalewayZoneSelect.addEventListener('change', updateDeployButtons);
    doRegionSelect.addEventListener('change', updateDeployButtons);
    scalewayDeployBtn.addEventListener('click', () => {
        if (!invokeNativeDeploy('scaleway')) {
            simulateDeploy('scaleway');
        }
    });
    doDeployBtn.addEventListener('click', () => {
        if (!invokeNativeDeploy('digitalocean')) {
            simulateDeploy('digitalocean');
        }
    });
    scalewayDestroyBtn.addEventListener('click', () => {
        if (!invokeNativeDestroy('scaleway')) {
            simulateDestroy();
        }
    });
    doDestroyBtn.addEventListener('click', () => {
        if (!invokeNativeDestroy('digitalocean')) {
            simulateDestroy();
        }
    });

    updateDeployButtons();
    resetStatus();
    renderQrCode(DEFAULT_WIREGUARD_CONFIG);
}

document.addEventListener('DOMContentLoaded', async () => {
    try {
        if (!window.QRCode) {
            await loadExternalScript(QR_CODE_CDN);
        }
    } catch (error) {
        console.warn(error);
    }

    initialize();
});