from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles  
import asyncio
from pathlib import Path
import logging
import json
from typing import Optional
import os

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

class StreamManager:
    def __init__(self):
        self.active_connections: set[WebSocket] = set()
        self.queue: list[str] = []
        self.current_task: Optional[asyncio.Task] = None
        self.is_streaming = False
        self.stream_event = asyncio.Event()
        self.current_file: Optional[str] = None
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info(f"New connection established. Total connections: {len(self.active_connections)}")
        if self.current_file:
            await self.broadcast_message(f"Currently playing: {self.current_file}")
        if self.queue:
            await self.broadcast_message(f"Queue: {', '.join(self.queue)}")
        
    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info(f"Connection closed. Remaining connections: {len(self.active_connections)}")
        
    async def broadcast_chunk(self, chunk: bytes, is_header: bool = False):
        for connection in self.active_connections:
            try:
                await connection.send_json({
                    "type": "header" if is_header else "chunk",
                    "size": len(chunk)
                })
                await connection.send_bytes(chunk)
            except Exception as e:
                logger.error(f"Error sending chunk: {e}")
                
    async def broadcast_message(self, message: str):
        logger.info(f"Broadcasting message: {message}")
        for connection in self.active_connections:
            try:
                await connection.send_json({
                    "type": "message", 
                    "content": message,
                    "queue": self.queue,
                    "current_file": self.current_file
                })
            except Exception as e:
                logger.error(f"Error sending message: {e}")

    async def add_file(self, file_path: str):
        """Add a file to queue and signal streaming if needed."""
        logger.info(f"Attempting to add file: {file_path}")
        # Resolve the file path relative to the current working directory
        abs_file_path = Path(os.getcwd()) / file_path
        
        if not abs_file_path.exists():
            logger.error(f"File not found: {abs_file_path}")
            await self.broadcast_message(f"Error: File not found - {file_path}")
            return False
            
        if not abs_file_path.is_file():
            logger.error(f"Not a file: {abs_file_path}")
            await self.broadcast_message(f"Error: Not a file - {file_path}")
            return False
            
        try:
            # Try to open the file to verify it's accessible
            with open(abs_file_path, 'rb') as f:
                f.read(1)
                
            self.queue.append(str(abs_file_path))
            logger.info(f"Successfully added file to queue: {file_path}")
            await self.broadcast_message(f"Added {file_path} to queue")
            self.stream_event.set()
            
            # Start streaming if not already active
            if not self.is_streaming:
                logger.info("Starting streaming task")
                if self.current_task:
                    self.current_task.cancel()
                self.current_task = asyncio.create_task(self.stream_queue())
            return True
            
        except Exception as e:
            logger.error(f"Error accessing file {abs_file_path}: {e}")
            await self.broadcast_message(f"Error: Cannot access file - {file_path}")
            return False
                
    async def stream_queue(self):
        """Continuously stream files from queue as they become available."""
        self.is_streaming = True
        logger.info("Starting queue streaming")
        try:
            while True:
                if not self.queue:
                    logger.info("Queue empty, waiting for new files...")
                    self.stream_event.clear()
                    self.current_file = None
                    await self.broadcast_message("Waiting for more files...")
                    await self.stream_event.wait()
                    continue
                
                self.current_file = self.queue.pop(0)
                logger.info(f"Now streaming: {self.current_file}")
                await self.broadcast_message(f"Now playing: {self.current_file}")
                await self.stream_file(self.current_file)
                
        except asyncio.CancelledError:
            logger.info("Streaming cancelled")
            await self.broadcast_message("Streaming cancelled")
            raise
        finally:
            self.is_streaming = False
            self.current_file = None

    async def stream_file(self, file_path: str):
        """Stream a single file with proper MSE handling."""
        chunk_size = 128 * 1024  # 128KB chunks
        try:
            with open(file_path, "rb") as f:
                logger.info(f"Starting to stream file: {file_path}")
                # Send file header
                header = f.read(10240)
                await self.broadcast_chunk(header, is_header=True)
                
                # Stream full file
                f.seek(0)
                chunks_sent = 0
                while chunk := f.read(chunk_size):
                    await self.broadcast_chunk(chunk)
                    chunks_sent += 1
                    if chunks_sent % 100 == 0:  # Log every 100 chunks
                        logger.info(f"Sent {chunks_sent} chunks of {file_path}")
                    await asyncio.sleep(0.01)
                    
                logger.info(f"Finished streaming file: {file_path}")
                await self.broadcast_message(f"Finished playing: {file_path}")
                    
        except Exception as e:
            logger.error(f"Error streaming file {file_path}: {e}")
            await self.broadcast_message(f"Error streaming {file_path}: {str(e)}")

manager = StreamManager()


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def get():
    with open(os.path.join("static", "index.html")) as f:
        return f.read()
    
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            logger.info(f"Received WebSocket message: {data}")
            try:
                command = json.loads(data)
                if command.get("action") == "add_file":
                    file_path = command.get("file_path")
                    logger.info(f"Processing add_file command for: {file_path}")
                    await manager.add_file(file_path)
            except json.JSONDecodeError as e:
                logger.error(f"Error decoding WebSocket message: {e}")
                continue
            except Exception as e:
                logger.error(f"Error processing WebSocket message: {e}")
                continue
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"Unexpected WebSocket error: {e}")

@app.post("/queue/add")
async def add_to_queue(file_path: str):
    """Add a file to the streaming queue."""
    logger.info(f"Received POST request to add file: {file_path}")
    success = await manager.add_file(file_path)
    return {
        "status": "success" if success else "error",
        "message": f"Added {file_path} to queue" if success else f"Failed to add {file_path}"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=4001)
