import logging

from langgraph.checkpoint.memory import MemorySaver

_LOGGER = logging.getLogger(__name__)
audit = logging.getLogger('aif_audit')

from .graph_builder import build_graph


checkpointer = MemorySaver()

try:
    compiled_graph = build_graph(checkpointer=checkpointer)
except Exception as e:
    _LOGGER.error(f"Error due compilation graph: {e}")
    audit.info({"code": "C4_FAIL_SERVICE_ACTION", "params": {"object_name": f"Error due compilation graph: {e}"}})
