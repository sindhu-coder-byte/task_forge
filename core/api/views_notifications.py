from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Notification


class NotificationListView(APIView):
    """Mirrors core.views.get_notifications: the 10 most recent unread
    notifications plus the true unread count (not just len(list))."""

    def get(self, request):
        base_qs = Notification.objects.filter(user=request.user, is_read=False)
        total_unread = base_qs.count()
        notifications = base_qs.order_by('-created_at')[:10]

        return Response({
            'count': total_unread,
            'notifications': [
                {
                    'id': n.id,
                    'notification_type': n.notification_type,
                    'is_read': n.is_read,
                    'title': n.title,
                    'message': n.message,
                    'created_at': n.created_at.isoformat(),
                    'task_id': n.task_id,
                    'project_id': n.project_id,
                }
                for n in notifications
            ],
        })


class NotificationHistoryView(APIView):
    """Mirrors core.views.notification_history — full paginated history,
    not just the unread-only feed NotificationListView returns."""

    def get(self, request):
        page = int(request.query_params.get('page', 1))
        page_size = 20
        qs = Notification.objects.filter(user=request.user).order_by('-created_at')
        total = qs.count()
        start = (page - 1) * page_size
        items = qs[start:start + page_size]

        return Response({
            'count': total,
            'page': page,
            'num_pages': max(1, (total + page_size - 1) // page_size),
            'notifications': [
                {
                    'id': n.id,
                    'notification_type': n.notification_type,
                    'is_read': n.is_read,
                    'title': n.title,
                    'message': n.message,
                    'created_at': n.created_at.isoformat(),
                    'task_id': n.task_id,
                    'project_id': n.project_id,
                }
                for n in items
            ],
        })


class MarkNotificationReadView(APIView):
    def post(self, request, notification_id):
        notification = Notification.objects.filter(id=notification_id, user=request.user).first()
        if notification is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        notification.is_read = True
        notification.save(update_fields=['is_read'])
        remaining = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response({'status': 'ok', 'remaining_count': remaining})


class MarkAllNotificationsReadView(APIView):
    def post(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({'status': 'ok'})
