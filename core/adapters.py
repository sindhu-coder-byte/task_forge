import traceback

from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth.models import User
from .models import Profile


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):

    def on_authentication_error(self, request, provider, error=None, exception=None, extra_context=None):
        """Log the exact exception so we can diagnose the 401."""
        print(f"[TF-OAUTH] on_authentication_error: error={error!r} exc_type={type(exception).__name__} exc={exception!r}")
        if exception is not None:
            traceback.print_exception(type(exception), exception, exception.__traceback__)
        super().on_authentication_error(request, provider, error=error, exception=exception, extra_context=extra_context)


    def send_notification_mail(self, template_prefix, user, context=None, email_address=None):
        # allauth 65 calls this inside SocialLogin.connect() — an SMTP/API
        # failure here would otherwise result in a 500 on the callback URL.
        try:
            super().send_notification_mail(template_prefix, user, context, email_address)
        except Exception as e:
            print(f"[TF-OAUTH] send_notification_mail suppressed: {e}")

    def pre_social_login(self, request, sociallogin):
        """
        Called after Google redirects back, before allauth completes login.
        For existing users: link their account + sync profile + apply invites.
        For new users: do nothing — save_user() handles them.

        The ENTIRE method is wrapped so no exception can return a 500.
        """
        try:
            email = sociallogin.account.extra_data.get('email')
            if not email:
                return

            user = User.objects.filter(email__iexact=email).first()
            if not user:
                return  # new user — let allauth create via save_user()

            # Only connect when this is the FIRST time the social account is being
            # linked (pk is None = new SocialAccount record). For returning Google
            # users the SocialAccount already has a pk; lookup() already set
            # sociallogin.user, so _login() handles the rest without a re-connect.
            if sociallogin.account.pk is None:
                try:
                    sociallogin.connect(request, user)
                except Exception as e:
                    print(f"[TF-OAUTH] sociallogin.connect() non-fatal: {e}")
                    sociallogin.user = user
            else:
                sociallogin.user = user

            # Sync OAuth provider info to the profile
            try:
                profile, _ = Profile.objects.get_or_create(user=user)
                profile.oauth_provider = sociallogin.account.provider
                profile.oauth_id = sociallogin.account.uid
                profile.save()
            except Exception as e:
                print(f"[TF-OAUTH] profile sync failed for {email}: {e}")

            # Apply any pending project invites sent to this email
            try:
                self._apply_pending_invites(user)
            except Exception as e:
                print(f"[TF-OAUTH] _apply_pending_invites failed for {email}: {e}")

        except Exception as e:
            # Absolute catch-all — never let pre_social_login raise a 500
            print(f"[TF-OAUTH] pre_social_login unexpected error: {e}")

    def save_user(self, request, sociallogin, form=None):
        """Called by allauth when creating a brand-new user via OAuth."""
        user = super().save_user(request, sociallogin, form)

        try:
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.oauth_provider = sociallogin.account.provider
            profile.oauth_id = sociallogin.account.uid
            if not profile.role:
                profile.role = 'user'
            profile.save()
        except Exception as e:
            print(f"[TF-OAUTH] save_user profile error for {user.email}: {e}")

        try:
            self._apply_pending_invites(user)
        except Exception as e:
            print(f"[TF-OAUTH] save_user _apply_pending_invites error for {user.email}: {e}")

        return user

    # ------------------------------------------------------------------
    # Shared helper — apply ALL unused invites for a user's email
    # ------------------------------------------------------------------
    def _apply_pending_invites(self, user):
        from .models import ProjectInvite, ProjectMembership
        from .notifications import NotificationService

        allowed_roles = [
            'project_lead', 'developer', 'tester',
            'qa', 'deployment_team', 'delivery_team', 'ui_ux_designer',
        ]

        pending = ProjectInvite.objects.filter(
            email__iexact=user.email,
            used=False,
        ).select_related('project', 'project__project_lead', 'team')

        service = NotificationService()

        for invite in pending:
            # Each invite is processed independently so one failure doesn't
            # block the others.
            try:
                invite.project.members.add(user)

                if invite.role in allowed_roles:
                    ProjectMembership.objects.get_or_create(
                        user=user,
                        project=invite.project,
                        defaults={'role': invite.role},
                    )

                if invite.team:
                    invite.team.members.add(user)

                invite.used = True
                invite.save()

                added_by = invite.project.project_lead or user
                try:
                    service.notify_project_member_added(
                        invite.project, user, invite.role, added_by, invite.team,
                    )
                except Exception as e:
                    print(f"[TF-OAUTH] notify_project_member_added failed: {e}")

            except Exception as e:
                print(f"[TF-OAUTH] invite {invite.id} processing failed: {e}")
