#!/usr/bin/env python3
"""
Simple development server for frontend work.
Serves static files and provides mock endpoints.
"""

import json
import os
from aiohttp import web, WSMsgType
from pathlib import Path

async def index_handler(request):
    """Serve the main HTML file"""
    static_dir = Path(__file__).parent / "static"
    with open(static_dir / "index.html", 'r', encoding='utf-8') as f:
        content = f.read()
    return web.Response(text=content, content_type='text/html')

async def static_handler(request):
    """Serve static files"""
    filename = request.match_info['filename']
    static_dir = Path(__file__).parent / "static"
    file_path = static_dir / filename
    
    print(f"📁 Static file requested: {filename}")
    print(f"📂 Looking for: {file_path}")
    
    if not file_path.exists():
        print(f"❌ File not found: {file_path}")
        return web.Response(status=404, text="File not found")
    
    print(f"✅ Serving: {file_path}")
    
    # Determine content type
    content_type = "text/plain"
    if filename.endswith('.css'):
        content_type = "text/css"
    elif filename.endswith('.js'):
        content_type = "application/javascript"
    elif filename.endswith('.png'):
        content_type = "image/png"
    elif filename.endswith('.jpg') or filename.endswith('.jpeg'):
        content_type = "image/jpeg"
    elif filename.endswith('.svg'):
        content_type = "image/svg+xml"
    
    if content_type.startswith('text/') or content_type == 'application/javascript':
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        return web.Response(text=content, content_type=content_type)
    else:
        with open(file_path, 'rb') as f:
            content = f.read()
        return web.Response(body=content, content_type=content_type)

async def websocket_handler(request):
    """Mock WebSocket handler for /realtime"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    print("WebSocket connection opened (mock)")
    
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
                print(f"Received: {data.get('type', 'unknown')}")
                
                # Mock responses for different message types
                if data.get('type') == 'session.update':
                    response = {
                        "type": "session.updated",
                        "session": {
                            "id": "mock_session_123",
                            "model": "gpt-4o-realtime-preview",
                            "modalities": ["text", "audio"],
                            "instructions": "Mock development mode",
                            "voice": data.get('session', {}).get('voice', 'alloy'),
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "input_audio_transcription": {"model": "whisper-1"},
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 200
                            },
                            "tools": [],
                            "tool_choice": "auto",
                            "temperature": 0.8,
                            "max_response_output_tokens": 4096
                        }
                    }
                    await ws.send_str(json.dumps(response))
                
            except json.JSONDecodeError:
                print("Invalid JSON received")
        elif msg.type == WSMsgType.ERROR:
            print(f'WebSocket error: {ws.exception()}')
    
    print("WebSocket connection closed")
    return ws

async def update_voice_handler(request):
    """Mock voice update endpoint"""
    data = await request.json()
    voice = data.get('voice', 'alloy')
    print(f"Voice updated to: {voice}")
    return web.json_response({"status": "success", "voice": voice})

async def outbound_call_handler(request):
    """Mock outbound call handler"""
    data = await request.json()
    phone_number = data.get('phone_number', '')
    print(f"Mock call to: {phone_number}")
    return web.json_response({
        "status": "success", 
        "message": f"Mock call initiated to {phone_number}",
        "call_id": "mock_call_123"
    })

def create_app():
    """Create the development app"""
    app = web.Application()
    
    # Routes
    app.router.add_get("/", index_handler)
    app.router.add_get("/static/{filename}", static_handler)
    app.router.add_get("/realtime", websocket_handler)
    app.router.add_post("/update-voice", update_voice_handler)
    app.router.add_post("/acs", outbound_call_handler)
    
    return app

if __name__ == "__main__":
    print("🚀 Starting development server...")
    print("📱 Frontend available at: http://localhost:8001")
    print("🔧 Development mode - no Azure backend required")
    print("✨ Perfect for UI/frontend development!")
    
    web.run_app(create_app(), host="0.0.0.0", port=8001)
