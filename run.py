"""
Run the PBX microservice application.

Usage:
    python run.py
    
    # Or with custom settings
    python run.py --host 0.0.0.0 --port 8000 --reload
"""

import uvicorn
import argparse


def main():
    """Run the application with uvicorn."""
    parser = argparse.ArgumentParser(description='Run PBX Microservice')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to')
    parser.add_argument('--port', type=int, default=8000, help='Port to bind to')
    parser.add_argument('--reload', action='store_true', help='Enable auto-reload')
    parser.add_argument('--workers', type=int, default=1, help='Number of worker processes')
    
    args = parser.parse_args()
    
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=args.workers if not args.reload else 1,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
