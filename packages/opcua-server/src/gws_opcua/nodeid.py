"""NodeId identifiers of the compatibility address space (ADR-0003).

Point NodeIds are `point:<exportPath>` and folder NodeIds are `folder:<exportPath>`. In the
point suffix, `%` is encoded as `%25` first, then `:` as `%3A`; every other character is kept,
so BrowseNames and folder paths still match the Ignition export exactly.
"""

NAMESPACE_URI = "urn:eetarp:graphene:demo:twin"
POINT_PREFIX = "point:"
FOLDER_PREFIX = "folder:"


def encode_point_path(export_path: str) -> str:
    return export_path.replace("%", "%25").replace(":", "%3A")


def decode_point_path(encoded: str) -> str:
    return encoded.replace("%3A", ":").replace("%25", "%")


def point_node_id(export_path: str) -> str:
    """String identifier of a point's NodeId, without the namespace index."""
    return POINT_PREFIX + encode_point_path(export_path)


def folder_node_id(export_path: str) -> str:
    return FOLDER_PREFIX + export_path


def export_path_of(node_id: str) -> str:
    """Inverse of `point_node_id`."""
    if not node_id.startswith(POINT_PREFIX):
        raise ValueError(f"not a point NodeId: {node_id!r}")
    return decode_point_path(node_id.removeprefix(POINT_PREFIX))
