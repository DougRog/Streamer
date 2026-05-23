#!/usr/bin/env python3
"""
Entry point for the Streamer MCR application.

Usage:
  python run.py

Environment variables (see config.py for full list):
  MEDIA_POOL_PATH   – path to MXF/media files (default: /mnt/MasterControlMedia)
  PORT              – HTTP port (default: 5001)
  HOST              – bind address (default: 0.0.0.0)
  DEBUG             – set to 'true' for Flask debug mode
  DATABASE_URL      – SQLAlchemy URL (default: sqlite:///streamer.db)
  DEKTEC_INPUT_URI  – override capture source (default: dektec:0:0)
  SECRET_KEY        – Flask secret (change in production)
"""
from app import application
import config

if __name__ == '__main__':
    application.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        use_reloader=False,   # reloader conflicts with background scheduler thread
    )
