from .backend.routes import register_routes
from .nodes import MusicVideoBuilder


NODE_CLASS_MAPPINGS = {
    "MusicVideoBuilder": MusicVideoBuilder,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MusicVideoBuilder": "Vesper Music Video Builder",
}

WEB_DIRECTORY = "./web"

register_routes()

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
