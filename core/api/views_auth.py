from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.core.validators import validate_email
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from core.api.serializers import UserSerializer
from core.models import Profile
from core.views import (
    _check_login_attempts,
    _clear_login_attempts,
    _increment_login_attempts,
)


class LoginView(APIView):
    """Username/email + password login for the native app, issuing a JWT
    pair instead of a session cookie. Mirrors core.views.login_view's
    validation (rate limiting, email-or-username lookup, email verification)
    so the app and the web login enforce identical rules.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        if _check_login_attempts(request):
            return Response(
                {'error': 'Too many login attempts. Please try again in 15 minutes.'},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        raw_login = (request.data.get('username') or '').strip()
        password = (request.data.get('password') or '').strip()

        if not raw_login or not password:
            return Response(
                {'error': 'Username/email and password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        username = raw_login
        if '@' in raw_login:
            try:
                validate_email(raw_login)
                matched_user = User.objects.filter(email__iexact=raw_login).first()
                if matched_user:
                    username = matched_user.username
            except ValidationError:
                return Response({'error': 'Invalid email format.'}, status=status.HTTP_400_BAD_REQUEST)

        user = authenticate(request, username=username, password=password)

        if user is None:
            _increment_login_attempts(request)
            return Response({'error': 'Invalid username/email or password.'}, status=status.HTTP_401_UNAUTHORIZED)

        profile, _created = Profile.objects.get_or_create(user=user)
        if not profile.email_verified:
            return Response(
                {'error': 'Please verify your email before signing in.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        _clear_login_attempts(request)

        refresh = RefreshToken.for_user(user)
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        })


class TokenExchangeView(APIView):
    """Exchanges the short-lived signed token from mobile_auth_complete for a JWT pair.

    Mirrors core.views.mobile_session_handoff's token verification, but issues a
    JWT pair instead of calling login()/setting a session cookie — the native
    app has no shared WebView cookie jar for a session cookie to land in.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get('token', '')
        signer = TimestampSigner()
        try:
            user_id = signer.unsign(token, max_age=60)
            user = User.objects.get(pk=user_id, is_active=True)
        except (BadSignature, SignatureExpired, User.DoesNotExist):
            return Response({'error': 'Invalid or expired token'}, status=status.HTTP_401_UNAUTHORIZED)

        refresh = RefreshToken.for_user(user)
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': UserSerializer(user).data,
        })


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)
