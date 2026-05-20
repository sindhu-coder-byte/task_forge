from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth.models import User
from .models import Profile


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):

    def send_notification_mail(self, template_prefix, user, context=None, email_address=None):
        # Swallow email errors so they never crash the OAuth callback.
        # allauth 65 calls this inside SocialLogin.connect() — an SMTP/API
        # failure here would otherwise result in a 500 on the callback URL.
        try:
            super().send_notification_mail(template_prefix, user, context, email_address)
        except Exception:
            pass

    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get('email')

        if not email:
            return

        user = User.objects.filter(email__iexact=email).first()

        # =========================
        # ✅ EXISTING USER
        # =========================
        if user:
            try:
                sociallogin.connect(request, user)
            except Exception:
                # allauth 65: connect() fires send_notification_mail() and
                # social_account_added signal — any failure must not 500 here.
                pass

            profile, _ = Profile.objects.get_or_create(user=user)
            profile.oauth_provider = sociallogin.account.provider
            profile.oauth_id = sociallogin.account.uid
            profile.save()

            # Apply any pending invites for this existing user's email
            self._apply_pending_invites(user)

            return

        # ❗ DO NOT create user here
        # Let allauth handle creation


    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)

        # =========================
        # ✅ ENSURE PROFILE EXISTS
        # =========================
        profile, _ = Profile.objects.get_or_create(user=user)

        # ✅ Save OAuth info
        profile.oauth_provider = sociallogin.account.provider
        profile.oauth_id = sociallogin.account.uid

        # ✅ Default role
        if not profile.role:
            profile.role = 'user'

        profile.save()

        # Apply any pending invites for new user
        self._apply_pending_invites(user)

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
            # Add to project members
            invite.project.members.add(user)

            # Assign project membership role
            if invite.role in allowed_roles:
                ProjectMembership.objects.get_or_create(
                    user=user,
                    project=invite.project,
                    defaults={'role': invite.role},
                )

            # Add to team if specified
            if invite.team:
                invite.team.members.add(user)

            invite.used = True
            invite.save()

            # Send welcome email + in-app notification (same as accept_project_invite)
            added_by = invite.project.project_lead or user
            try:
                service.notify_project_member_added(
                    invite.project, user, invite.role, added_by, invite.team,
                )
            except Exception:
                pass