"""
title: Chat Image Retrieval
description: Retrieve a saved current-chat image only after explicit user approval.
version: 0.2.0
required_open_webui_version: 0.13.0
"""

import importlib
import sys

# Reload the mounted package in place when this tool module reloads,
# so package updates deploy without restarting the application.
if "chat_image_retrieval" in sys.modules:
    importlib.reload(sys.modules["chat_image_retrieval"])

from chat_image_retrieval import Tools
