import time


class OAuthStateFallbackMiddleware:
    """Restore allauth OAuth state from DB into the session before the callback view runs.

    Chrome drops the session cookie on the cross-site redirect from Google back
    to the local server.  When that happens, allauth can't find the state in the
    session and returns 401.

    This middleware detects the OAuth callback URL, checks whether the state is
    missing from the session, and if so restores it from the DB backup that was
    written during login initiation (by the stash patch in apps.py).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._maybe_restore_oauth_state(request)
        return self.get_response(request)

    def _maybe_restore_oauth_state(self, request):
        state_id = request.GET.get("state")
        if not state_id or "callback" not in request.path:
            return

        session_states = request.session.get("socialaccount_states") or {}
        in_session = state_id in session_states

        try:
            from core.models import OAuthState
            db_count = OAuthState.objects.filter(state_id=state_id).count()
        except Exception:
            db_count = "ERR"

        print(f"[OAuthFallback] callback: state_id={state_id!r} in_session={in_session} in_db={db_count}")

        if in_session:
            return

        try:
            from core.models import OAuthState
            obj = OAuthState.objects.filter(state_id=state_id).first()
            if obj:
                session_states[state_id] = (obj.state_data, time.time())
                request.session["socialaccount_states"] = session_states
                request.session.modified = True
                obj.delete()
                print(f"[OAuthFallback] Restored state {state_id!r} from DB into session")
            else:
                print(f"[OAuthFallback] state {state_id!r} NOT found in DB — 401 will follow")
        except Exception as exc:
            print(f"[OAuthFallback] Error: {exc}")
