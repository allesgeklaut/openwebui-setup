"""
title: Chat Image Retrieval Filter
description: Omit older image payloads and expose scoped references for approved retrieval.
version: 0.2.0
required_open_webui_version: 0.13.0
"""

import importlib

import chat_image_retrieval

# Reload the mounted package in place when this function module reloads, so
# package updates deploy without restarting the application.
chat_image_retrieval = importlib.reload(chat_image_retrieval)

from chat_image_retrieval import Filter
