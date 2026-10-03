#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compatibility wrapper for older installs that call apiclaude.py directly."""

from apiagent import main


if __name__ == "__main__":
    raise SystemExit(main())
