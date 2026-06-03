const QR_CODE_CDN = 'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js';

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
const qrRow = document.querySelector('.status-row-qr');
const qrContainer = document.getElementById('qrcode');

let activeProvider = 'scaleway';
let operationInFlight = false;

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
    activeProvider = provider;

    providerTabs.forEach((tab) => {
        tab.classList.toggle('active', tab.dataset.provider === provider);
    });

    scalewaySection.classList.toggle('hidden-section', provider !== 'scaleway');
    digitaloceanSection.classList.toggle('hidden-section', provider !== 'digitalocean');
}

function setProviderControlsDisabled(isDisabled) {
    providerTabs.forEach((tab) => {
        tab.disabled = isDisabled;
        tab.classList.toggle('is-disabled', isDisabled);
    });
}

function updateDeployButtons() {
    scalewayDeployBtn.disabled = operationInFlight || !scalewayZoneSelect.value;
    doDeployBtn.disabled = operationInFlight || !doRegionSelect.value;
}

function clearQrCode() {
    if (qrContainer) {
        qrContainer.replaceChildren();
    }
}

function showQrCodeRow() {
    if (!qrRow) {
        return;
    }

    qrRow.classList.remove('hidden-section');
    qrRow.setAttribute('aria-hidden', 'false');
}

function hideQrCodeRow() {
    clearQrCode();

    if (!qrRow) {
        return;
    }

    qrRow.classList.add('hidden-section');
    qrRow.setAttribute('aria-hidden', 'true');
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

function resetStatus() {
    statusCard.classList.remove('active');
    statusDot.className = 'status-indicator';
    statusText.textContent = 'Disconnected';
    ipAddress.textContent = '—';
    hideQrCodeRow();
    setProviderControlsDisabled(false);
    scalewayDestroyBtn.disabled = false;
    doDestroyBtn.disabled = false;
    updateDeployButtons();
}

function setConnectingState() {
    statusCard.classList.add('active');
    statusDot.className = 'status-indicator connecting';
    statusText.textContent = 'Connecting...';
    ipAddress.textContent = '—';
    showQrCodeRow();
    clearQrCode();
}

function setConnectedState(ip, configText) {
    statusCard.classList.add('active');
    statusDot.className = 'status-indicator connected';
    statusText.textContent = 'Connected';
    ipAddress.textContent = ip;
    showQrCodeRow();
    renderQrCode(configText);
}

function extractErrorMessage(resultOrError) {
    if (!resultOrError) {
        return 'Unknown error';
    }

    if (resultOrError instanceof Error) {
        return resultOrError.message;
    }

    if (typeof resultOrError === 'object' && 'message' in resultOrError && resultOrError.message) {
        return String(resultOrError.message);
    }

    return String(resultOrError);
}

async function invokeNativeDeploy(provider) {
    const nativeApi = window.pywebview?.api;

    if (!nativeApi?.deploy) {
        throw new Error('Native deploy API is not available');
    }

    const region = provider === 'scaleway' ? scalewayZoneSelect.value : doRegionSelect.value;
    return await nativeApi.deploy(provider, region);
}

async function invokeNativeDestroy(provider) {
    const nativeApi = window.pywebview?.api;

    if (!nativeApi?.destroy) {
        throw new Error('Native destroy API is not available');
    }

    return await nativeApi.destroy(provider);
}

async function handleDeploy(provider) {
    if (operationInFlight) {
        return;
    }

    operationInFlight = true;
    setProviderControlsDisabled(true);
    updateDeployButtons();
    scalewayDestroyBtn.disabled = true;
    doDestroyBtn.disabled = true;
    setConnectingState();

    try {
        const result = await invokeNativeDeploy(provider);

        if (result && result.status === 'success') {
            if (!result.ip || !result.config) {
                throw new Error('Deploy response missing ip or config');
            }

            setConnectedState(result.ip, result.config);
            scalewayDestroyBtn.disabled = false;
            doDestroyBtn.disabled = false;
            return;
        }

        throw new Error(extractErrorMessage(result) || 'Deployment failed');
    } catch (error) {
        const message = extractErrorMessage(error);
        console.error('Deployment failed:', message, error);
        alert(message);
        resetStatus();
    } finally {
        operationInFlight = false;
        setProviderControlsDisabled(false);
        updateDeployButtons();
    }
}

async function handleDestroy(provider) {
    if (operationInFlight) {
        return;
    }

    operationInFlight = true;
    setProviderControlsDisabled(true);
    updateDeployButtons();

    try {
        const result = await invokeNativeDestroy(provider);

        if (!result || result.status !== 'success') {
            throw new Error(extractErrorMessage(result) || 'Destroy failed');
        }

        if (provider === 'scaleway') {
            scalewayZoneSelect.value = '';
        } else {
            doRegionSelect.value = '';
        }

        resetStatus();
    } catch (error) {
        const message = extractErrorMessage(error);
        console.error('Destroy failed:', message, error);
        alert(message);
        resetStatus();
    } finally {
        operationInFlight = false;
        setProviderControlsDisabled(false);
        updateDeployButtons();
    }
}

function initialize() {
    providerTabs.forEach((tab) => {
        tab.addEventListener('click', () => setActiveProvider(tab.dataset.provider));
    });

    scalewayZoneSelect.addEventListener('change', updateDeployButtons);
    doRegionSelect.addEventListener('change', updateDeployButtons);

    scalewayDeployBtn.addEventListener('click', () => {
        handleDeploy('scaleway').catch((error) => {
            const message = extractErrorMessage(error);
            console.error('Deployment failed:', message, error);
            alert(message);
            resetStatus();
        });
    });

    doDeployBtn.addEventListener('click', () => {
        handleDeploy('digitalocean').catch((error) => {
            const message = extractErrorMessage(error);
            console.error('Deployment failed:', message, error);
            alert(message);
            resetStatus();
        });
    });

    scalewayDestroyBtn.addEventListener('click', () => {
        handleDestroy('scaleway').catch((error) => {
            const message = extractErrorMessage(error);
            console.error('Destroy failed:', message, error);
            alert(message);
            resetStatus();
        });
    });

    doDestroyBtn.addEventListener('click', () => {
        handleDestroy('digitalocean').catch((error) => {
            const message = extractErrorMessage(error);
            console.error('Destroy failed:', message, error);
            alert(message);
            resetStatus();
        });
    });

    setActiveProvider(activeProvider);
    resetStatus();
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