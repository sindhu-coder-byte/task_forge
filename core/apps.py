from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'

    def ready(self):
        import core.signals  # noqa: F401
        from django.db.models.signals import post_migrate
        post_migrate.connect(_sync_site_domain, sender=self)
        post_migrate.connect(_ensure_superuser, sender=self)
        post_migrate.connect(_cleanup_db_socialapps, sender=self)
        _patch_allauth_statekit()


def _cleanup_db_socialapps(**_kwargs):
    """Remove DB SocialApp records for providers configured in settings.

    Having both a settings-based APP and a DB SocialApp causes
    MultipleObjectsReturned in allauth's get_app(). Runs after migrate
    (not in ready()) to avoid the DB-access-during-init warning.
    """
    try:
        from allauth.socialaccount.models import SocialApp
        deleted, _ = SocialApp.objects.filter(provider='google').delete()
        if deleted:
            print(f"[core.apps] Removed {deleted} stale DB Google SocialApp record(s).")
    except Exception:
        pass


def _patch_allauth_statekit():
    """Patch allauth's statekit to persist OAuth state in the DB as a fallback.

    Chrome drops the session cookie during the cross-site redirect from Google
    back to http://127.0.0.1 or http://localhost.  By also writing state to the
    DB during stash and reading it back during unstash (only when the session
    misses), the OAuth callback always succeeds regardless of cookie behaviour.
    """
    try:
        from allauth.socialaccount.internal import statekit

        _orig_stash = statekit.stash_state
        _orig_unstash = statekit.unstash_state

        def _stash(request, state, state_id=None):
            sid = _orig_stash(request, state, state_id=state_id)
            try:
                import datetime
                from django.utils import timezone
                from core.models import OAuthState
                OAuthState.objects.filter(
                    created_at__lt=timezone.now() - datetime.timedelta(minutes=10)
                ).delete()
                OAuthState.objects.filter(state_id=sid).delete()
                OAuthState.objects.create(state_id=sid, state_data=state)
                print(f"[OAuthState] stashed state_id={sid!r} to DB")
            except Exception as exc:
                print(f"[OAuthState] ERROR saving state to DB: {exc}")
            return sid

        def _unstash(request, state_id):
            state = _orig_unstash(request, state_id)
            if state is None:
                try:
                    from core.models import OAuthState
                    obj = OAuthState.objects.filter(state_id=state_id).first()
                    if obj:
                        state = obj.state_data
                        obj.delete()
                        print(f"[OAuthState] recovered state_id={state_id!r} from DB fallback")
                    else:
                        print(f"[OAuthState] state_id={state_id!r} NOT found in session OR DB — 401")
                except Exception as exc:
                    print(f"[OAuthState] ERROR reading state from DB: {exc}")
            return state

        statekit.stash_state = _stash
        statekit.unstash_state = _unstash
    except ImportError:
        pass


def _sync_site_domain(**_kwargs):
    """Keep django.contrib.sites in sync with SITE_URL / RENDER_EXTERNAL_HOSTNAME."""
    import os
    from urllib.parse import urlparse
    from django.conf import settings
    from django.contrib.sites.models import Site

    site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
    render_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', '')

    if site_url:
        domain = urlparse(site_url).netloc
    elif render_host:
        domain = render_host
    else:
        return

    if domain:
        try:
            site_id = settings.SITE_ID
            # Remove domain from any other site record to avoid duplicate constraint
            Site.objects.exclude(pk=site_id).filter(domain=domain).update(domain=f'unused-{site_id}-{domain}')
            # Now create or update the target site with the correct domain
            Site.objects.update_or_create(
                pk=site_id,
                defaults={'domain': domain, 'name': domain},
            )
        except Exception as exc:
            print(f'[core.apps._sync_site_domain] ERROR syncing site domain: {exc}')


def _ensure_superuser(**_kwargs):
    """Create/repair superuser from env vars after every migrate run."""
    import os
    username = os.environ.get('DJANGO_SUPERUSER_USERNAME', '').strip()
    password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '').strip()
    email    = os.environ.get('DJANGO_SUPERUSER_EMAIL', '').strip()

    if not username or not password:
        return

    try:
        from django.contrib.auth.models import User
        from core.models import Profile

        user, created = User.objects.get_or_create(username=username)
        user.email = email
        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        profile, _ = Profile.objects.get_or_create(user=user)
        if profile.role != 'admin':
            profile.role = 'admin'
            profile.save(update_fields=['role'])

        verb = 'created' if created else 'updated'
        print(f'[ensure_superuser] Superuser "{username}" {verb} successfully.')
    except Exception as exc:
        print(f'[ensure_superuser] ERROR: {exc}')
