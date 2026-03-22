import sys
import os

# Add project root to path so fairing_mcp can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fairing_mcp import mcp

app = mcp.streamable_http_app()
