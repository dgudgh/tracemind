"""Host facade for the wire implementation shipped in signed Providers."""
from app.runtime.engine_sdk.wire import (
    IpcError, MAX_DEPTH, MAX_FRAME_BYTES, MAX_WIRE_BYTES, PeerCredentials,
    canonical, decode_frame, encode_frame, mac, peer_credentials, recv_exact,
    recv_frame, recv_plain, send_frame, send_plain, session_keys, strict_json,
    validate_tree, verify_mac,
)
_recv_exact = recv_exact
