# SPDX-License-Identifier: GPL-3.0-or-later
"""Yohsai pattern loading, sewing verification, Update, and Kitsuke tools."""

from __future__ import annotations

from . import ui


def register():
    ui.register()


def unregister():
    ui.unregister()
