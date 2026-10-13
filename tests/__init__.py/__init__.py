"""Isolate config before importing any Atticus test module."""
import atexit
import os
import tempfile

config = tempfile.TemporaryDirectory(prefix='atticus-test-config-')
os.environ['ATTICUS_DIR'] = config.name
atexit.register(config.cleanup)
