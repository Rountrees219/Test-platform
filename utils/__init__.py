"""
Ninja Squad AI Utilities
========================

Utility functions for interacting with AI models via the NinjaTech LiteLLM gateway.

Available modules:
    - litellm_client: Core client configuration and authentication
    - chat: Chat completion utilities (Claude, GPT, NinjaTech)
    - images: Image generation and editing utilities (gpt-image-2)
    - video: Video generation utilities (Seedance 2.5, via the video MCP server)
    - vision: Image and video understanding (GLM 5.3 Flash, via the vision MCP server)
    - embeddings: Text embedding utilities
    - mcp: MCP tool discovery (146 tools across 4 services)

Quick Start:
    from utils.chat import chat
    response = chat("What is 2+2?")
    print(response)

    from utils.images import generate_image, edit_image
    path = generate_image("A sunset over mountains")
    path = edit_image(
        "Image 1 is a chair. Image 2 is a cat. Put the cat on the chair.",
        references=["chair.png", "cat.png"],
    )

    from utils.video import submit_video, check_video, download_video
    video_id = submit_video("A cat playing with yarn")   # then poll check_video

    from utils.embeddings import embed
    vector = embed("Hello world")

MCP Tools (CLI):
    python -m utils.mcp list          # 146 tools across 4 services
    python -m utils.mcp search hotel  # search by name/description
    python -m utils.mcp groups        # booking_com, flights, linkedin
    python -m utils.mcp info <name>   # detailed tool info

MCP Tools (Python):
    from utils.mcp import MCPClient
    async with MCPClient() as client:
        tools = await client.list_tools()
"""

from clients.litellm_client import get_config
