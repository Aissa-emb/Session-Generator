"""Structured operational events with an explicit allowlist; no request bodies."""
import json
import logging
import sys
import time

logger=logging.getLogger('session_generator.audit')
if not logger.handlers:
    handler=logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate=False


def emit(event, **fields):
    allowed={'run_id','stream_id','status','spec_version','runner_region','sessions','payloads','errors'}
    logger.info(json.dumps({'time':time.time(),'event':event,**{k:v for k,v in fields.items() if k in allowed}},separators=(',',':')))
