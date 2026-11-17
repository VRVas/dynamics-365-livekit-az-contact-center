const toggleButton = document.getElementById('toggleButton');
const micButton = document.getElementById('micButton');
const callButton = document.getElementById('callButton');
const statusMessage = document.getElementById('statusMessage');
const warningMessage = document.getElementById('warningMessage');
const reportDiv = document.getElementById('report');
let runtimeConfig = null;
const languageSelect = document.getElementById('languageSelect');

async function loadRuntimeConfig() {
    try {
        const response = await fetch('/config', { cache: 'no-store' });
        if (!response.ok) throw new Error('Voice configuration is unavailable.');
        runtimeConfig = await response.json();
        languageSelect.value = runtimeConfig.organization.defaultLanguage;
        toggleButton.disabled = !runtimeConfig.features.browserVoice;
        callButton.disabled = !runtimeConfig.features.applicationTelephony;
        document.querySelectorAll('[data-feature]').forEach(element => {
            element.hidden = !runtimeConfig.features[element.dataset.feature];
        });
        statusMessage.textContent = runtimeConfig.features.browserVoice
            ? 'Ready' : 'Browser voice is disabled for this deployment.';
    } catch (error) {
        runtimeConfig = null;
        toggleButton.disabled = true;
        callButton.disabled = true;
        statusMessage.textContent = error.message || 'Invalid voice configuration.';
    }
}
loadRuntimeConfig();

let isRecording = false;
let isMuted = false;
let websocket = null;
let audioContext = null;
let mediaStream = null;
let mediaProcessor = null;
let audioQueueTime = 0;
let sessionReady = false;
let recordingGeneration = 0;

// Variables for client-side VAD (optional)
let speaking = false;
const VAD_THRESHOLD = 0.01; // Adjust this threshold as needed

async function startRecording() {
    if (!runtimeConfig?.features.browserVoice) throw new Error('Browser voice is disabled.');
    const generation = ++recordingGeneration;
    isRecording = true;
    sessionReady = false;
    warningMessage.textContent = '';
    warningMessage.hidden = true;
    isMuted = false;
    toggleButton.textContent = 'Stop Conversation';
    statusMessage.textContent = 'Connecting to HUMAIN...';
    document.getElementById('themeSelect').disabled = true;
    languageSelect.disabled = true;
    
    // Show the mic button
    micButton.style.display = 'inline-block';
    micButton.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
        <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
    </svg>`;
    micButton.classList.remove('muted');

    // Initialize AudioContext if not already done
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
        audioQueueTime = audioContext.currentTime;
    }
    await audioContext.resume();
    if (generation !== recordingGeneration) return;
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (generation !== recordingGeneration) {
        stream.getTracks().forEach(track => track.stop());
        return;
    }
    mediaStream = stream;

    // Open WebSocket connection
    const mainHost = window.location.host;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    websocket = new WebSocket(`${protocol}//${mainHost}/realtime`);
    const connection = websocket;

    websocket.onopen = () => {
        console.log('WebSocket connection opened');
        const voice = document.getElementById('themeSelect').value;
        const sessionUpdate = {
            type: 'session.update',
            session: { language: languageSelect.value, ...(voice ? { voice } : {}) }
        };
        connection.send(JSON.stringify(sessionUpdate));
    };

    websocket.onmessage = (event) => {
        if (websocket !== connection) return;
        try {
            handleWebSocketMessage(JSON.parse(event.data));
        } catch (error) {
            console.error('Invalid voice message:', error);
            stopRecording();
            statusMessage.textContent = 'Invalid message from the voice backend.';
        }
    };

    websocket.onclose = () => {
        console.log('WebSocket connection closed');
        if (websocket === connection && isRecording) {
            stopRecording();
            statusMessage.textContent = 'Voice session disconnected. Please start again.';
        }
    };

    websocket.onerror = (event) => {
        console.error('WebSocket error:', event);
        if (websocket === connection) {
            stopRecording();
            statusMessage.textContent = 'Could not connect to the voice backend.';
        }
    };

    // Start recording audio
    const source = audioContext.createMediaStreamSource(mediaStream);

    mediaProcessor = audioContext.createScriptProcessor(4096, 1, 1);
    source.connect(mediaProcessor);
    mediaProcessor.connect(audioContext.destination);

    mediaProcessor.onaudioprocess = (e) => {
        if (!sessionReady || connection.readyState !== WebSocket.OPEN) return;
        const inputData = e.inputBuffer.getChannelData(0);
        
        // Silence keeps the media clock running while the microphone is muted.
        const int16Data = isMuted ? new Int16Array(inputData.length) : float32ToInt16(inputData);
        const audioCommand = {
            type: 'input_audio_buffer.append',
            audio: int16ToBase64(int16Data)
        };
        connection.send(JSON.stringify(audioCommand));

        // Optional: Client-side VAD for immediate interruption handling (can be removed, as we now handle the "input_audio_buffer.speech_started" event)
        // const isUserSpeaking = detectSpeech(inputData);
        // if (isUserSpeaking && !speaking) {
        //     speaking = true;
        //     console.log('User started speaking');
        //     // Stop assistant's audio playback
        //     //stopAssistantAudio();
        // } else if (!isUserSpeaking && speaking) {
        //     speaking = false;
        //     console.log('User stopped speaking');
        // }
    };
}

function stopRecording() {
    recordingGeneration += 1;
    sessionReady = false;
    warningMessage.textContent = '';
    warningMessage.hidden = true;
    isRecording = false;
    isMuted = false;
    toggleButton.textContent = 'Live in-app AI Agent';
    statusMessage.textContent = 'Stopped';
    document.getElementById('themeSelect').disabled = false;
    languageSelect.disabled = false;
    
    // Hide the mic button
    micButton.style.display = 'none';

    stopAssistantAudio();

    if (mediaProcessor) {
        mediaProcessor.disconnect();
        mediaProcessor.onaudioprocess = null;
        mediaProcessor = null;
    }

    if (mediaStream) {
        mediaStream.getTracks().forEach(track => track.stop());
        mediaStream = null;
    }

    if (websocket) {
        websocket.onopen = null;
        websocket.onmessage = null;
        websocket.onclose = null;
        websocket.onerror = null;
        websocket.close();
        websocket = null;
    }
    if (audioContext && audioContext.state !== 'closed') {
        audioContext.close().catch(error => console.error('Audio cleanup failed:', error));
    }
    audioContext = null;
}

function onToggleListening() {
    if (!isRecording) {
        const pending = startRecording();
        const generation = recordingGeneration;
        pending.catch(error => {
            if (generation !== recordingGeneration) return;
            console.error('Microphone setup failed:', error);
            stopRecording();
            statusMessage.textContent = error.message || 'Microphone setup failed.';
        });
    } else {
        stopRecording();
    }
}

function toggleMicrophone() {
    if (!isRecording) return; // Only work when recording is active
    
    isMuted = !isMuted;
    
    if (isMuted) {
        // Muted microphone with slash
        micButton.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
            <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
            <line x1="3" y1="3" x2="21" y2="21" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>`;
        micButton.classList.add('muted');
        statusMessage.textContent = 'Muted';
    } else {
        // Active microphone
        micButton.innerHTML = `<svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"/>
            <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/>
        </svg>`;
        micButton.classList.remove('muted');
        statusMessage.textContent = 'Listening...';
    }
}

async function onCallButton() {
    if (!runtimeConfig?.features.applicationTelephony) {
        statusMessage.textContent = 'Application telephony is disabled.';
        return;
    }
    const phonenumber = document.getElementById('phonenumber').value;
    try {
        const response = await fetch(`${window.location.origin}/call`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ number: phonenumber, language: languageSelect.value })
        });
        statusMessage.textContent = await response.text();
    } catch {
        statusMessage.textContent = 'Call outcome is unknown. Do not retry automatically.';
    }
}

toggleButton.addEventListener('click', onToggleListening);
micButton.addEventListener('click', toggleMicrophone);
languageSelect.addEventListener('change', () => {
    if (sessionReady && websocket?.readyState === WebSocket.OPEN) {
        websocket.send(JSON.stringify({ type: 'session.language', language: languageSelect.value }));
    }
});

if (callButton) {
  callButton.addEventListener('click', onCallButton);
}

function handleWebSocketMessage(message) {
    switch (message.type) {
        case 'session.updated':
            sessionReady = true;
            languageSelect.disabled = false;
            statusMessage.textContent = 'Listening';
            break;
        case 'session.warning':
            warningMessage.textContent = message.warning?.message || '';
            warningMessage.hidden = !warningMessage.textContent;
            break;
        case 'response.output_audio.delta':
        case 'response.audio.delta':
            // Play audio delta
            if (message.delta) {
                playAudio(message.delta);
            }
            break;
        case 'response.done':
            // Conversation response is complete
            console.log('Response done');
            break;
        case 'extension.middle_tier_tool_response':
            // Handle tool response
            if (message.tool_name === 'generate_report') {
                const report = JSON.parse(message.tool_result);
                displayReport(report);
            }
            break;
        case "input_audio_buffer.speech_started":
            // User started speaking and interrupted the AI
            // In this case, we don't want to send the unplayed audio buffer to the client anymore and clear the buffer audio.
            stopAssistantAudio();
            break;
        case 'error':
            console.error('Error message from server:', JSON.stringify(message, null, 2));
            stopRecording();
            statusMessage.textContent = message.error?.message || 'Voice backend error.';
            break;
        default:
            console.log('Unhandled message type:', message.type);
    }
}

let assistantAudioSources = [];

function playAudio(base64Audio) {
    const binary = atob(base64Audio);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    const int16Array = new Int16Array(bytes.buffer);

    // Convert Int16Array to Float32Array
    const float32Array = int16ToFloat32(int16Array);

    // Create an AudioBuffer and play it
    if (!audioContext) {
        audioContext = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 });
        audioQueueTime = audioContext ? audioContext.currentTime : 0;
    }

    const audioBuffer = audioContext.createBuffer(1, float32Array.length, 24000);
    audioBuffer.copyToChannel(float32Array, 0);

    const source = audioContext.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(audioContext.destination);

    // Schedule the audio chunk to play at the correct time
    const currentTime = audioContext.currentTime;
    const startTime = Math.max(audioQueueTime, currentTime + 0.1); // Slight delay to prevent overlap
    source.start(startTime);

    // Keep track of audio sources
    assistantAudioSources.push(source);

    // Update the audioQueueTime to the end of this buffer
    audioQueueTime = startTime + audioBuffer.duration;

    source.onended = () => {
        // Remove source from array when it finishes playing
        assistantAudioSources = assistantAudioSources.filter(s => s !== source);
    };
}

function stopAssistantAudio() {
    // Stop all assistant audio sources
    assistantAudioSources.forEach(source => {
        try {
            source.stop();
        } catch (e) {
            console.error('Error stopping audio source:', e);
        }
    });
    assistantAudioSources = [];
    audioQueueTime = audioContext ? audioContext.currentTime : 0;
}

function displayReport(report) {
    reportDiv.textContent = JSON.stringify(report, null, 2);
}

function float32ToInt16(float32Array) {
    const int16Array = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
        let s = Math.max(-1, Math.min(1, float32Array[i]));
        int16Array[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
    }
    return int16Array;
}

function int16ToBase64(int16Array) {
    const byteArray = new Uint8Array(int16Array.buffer);
    let binary = '';
    for (let i = 0; i < byteArray.byteLength; i++) {
        binary += String.fromCharCode(byteArray[i]);
    }
    return btoa(binary);
}

function int16ToFloat32(int16Array) {
    const float32Array = new Float32Array(int16Array.length);
    for (let i = 0; i < int16Array.length; i++) {
        let int = int16Array[i];
        // Convert back to float
        let float = int < 0 ? int / 0x8000 : int / 0x7FFF;
        float32Array[i] = float;
    }
    return float32Array;
}

function detectSpeech(inputData) {
    // Calculate the root mean square (RMS) of the inputData
    let sumSquares = 0;
    for (let i = 0; i < inputData.length; i++) {
        sumSquares += inputData[i] * inputData[i];
    }
    const rms = Math.sqrt(sumSquares / inputData.length);
    return rms > VAD_THRESHOLD;
}
