''' test incoming activities '''
from unittest.mock import patch

from django.http import HttpResponseBadRequest, HttpResponseNotAllowed, \
        HttpResponseNotFound
from django.test import TestCase
from django.test.client import RequestFactory

from bookwyrm import models, incoming


class Incoming(TestCase):
    ''' a lot here: all handlers for receiving activitypub requests '''
    def setUp(self):
        ''' we need basic things, like users '''
        self.local_user = models.User.objects.create_user(
            'mouse', 'mouse@mouse.com', 'mouseword', local=True)
        self.local_user.remote_id = 'http://local.com/user/mouse'
        self.local_user.save()
        with patch('bookwyrm.models.user.set_remote_server.delay'):
            self.remote_user = models.User.objects.create_user(
                'rat', 'rat@rat.com', 'ratword',
                local=False,
                remote_id='https://example.com/users/rat',
                inbox='https://example.com/users/rat/inbox',
                outbox='https://example.com/users/rat/outbox',
            )
        self.status = models.Status.objects.create(
            user=self.local_user,
            content='Test status',
            remote_id='http://local.com/status/1',
        )
        self.factory = RequestFactory()


    def test_inbox_invalid_get(self):
        ''' shouldn't try to handle if the user is not found '''
        request = self.factory.get('http://www.example.com/')
        self.assertIsInstance(
            incoming.inbox(request, 'anything'), HttpResponseNotAllowed)
        self.assertIsInstance(
            incoming.shared_inbox(request), HttpResponseNotAllowed)

    def test_inbox_invalid_user(self):
        ''' shouldn't try to handle if the user is not found '''
        request = self.factory.post('http://www.example.com/')
        self.assertIsInstance(
            incoming.inbox(request, 'fish@tomato.com'), HttpResponseNotFound)

    def test_inbox_invalid_no_object(self):
        ''' json is missing "object" field '''
        request = self.factory.post(
            self.local_user.shared_inbox, data={})
        self.assertIsInstance(
            incoming.shared_inbox(request), HttpResponseBadRequest)

    def test_inbox_invalid_bad_signature(self):
        ''' bad request for invalid signature '''
        request = self.factory.post(
            self.local_user.shared_inbox,
            '{"type": "Test", "object": "exists"}',
            content_type='application/json')
        with patch('bookwyrm.incoming.has_valid_signature') as mock_has_valid:
            mock_has_valid.return_value = False
            self.assertEqual(
                incoming.shared_inbox(request).status_code, 401)

    def test_inbox_invalid_bad_signature_delete(self):
        ''' invalid signature for Delete is okay though '''
        request = self.factory.post(
            self.local_user.shared_inbox,
            '{"type": "Delete", "object": "exists"}',
            content_type='application/json')
        with patch('bookwyrm.incoming.has_valid_signature') as mock_has_valid:
            mock_has_valid.return_value = False
            self.assertEqual(
                incoming.shared_inbox(request).status_code, 200)

    def test_inbox_unknown_type(self):
        ''' never heard of that activity type, don't have a handler for it '''
        request = self.factory.post(
            self.local_user.shared_inbox,
            '{"type": "Fish", "object": "exists"}',
            content_type='application/json')
        with patch('bookwyrm.incoming.has_valid_signature') as mock_has_valid:
            mock_has_valid.return_value = True
            self.assertIsInstance(
                incoming.shared_inbox(request), HttpResponseNotFound)

    def test_inbox_success(self):
        ''' a known type, for which we start a task '''
        request = self.factory.post(
            self.local_user.shared_inbox,
            '{"type": "Accept", "object": "exists"}',
            content_type='application/json')
        with patch('bookwyrm.incoming.has_valid_signature') as mock_has_valid:
            mock_has_valid.return_value = True

            with patch('bookwyrm.incoming.handle_follow_accept.delay'):
                self.assertEqual(
                    incoming.shared_inbox(request).status_code, 200)


    def test_handle_follow(self):
        ''' remote user wants to follow local user '''
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.com/users/rat/follows/123",
            "type": "Follow",
            "actor": "https://example.com/users/rat",
            "object": "http://local.com/user/mouse"
        }

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            incoming.handle_follow(activity)

        # notification created
        notification = models.Notification.objects.get()
        self.assertEqual(notification.user, self.local_user)
        self.assertEqual(notification.notification_type, 'FOLLOW')

        # the request should have been deleted
        requests = models.UserFollowRequest.objects.all()
        self.assertEqual(list(requests), [])

        # the follow relationship should exist
        follow = models.UserFollows.objects.get(user_object=self.local_user)
        self.assertEqual(follow.user_subject, self.remote_user)


    def test_handle_follow_manually_approved(self):
        ''' needs approval before following '''
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.com/users/rat/follows/123",
            "type": "Follow",
            "actor": "https://example.com/users/rat",
            "object": "http://local.com/user/mouse"
        }

        self.local_user.manually_approves_followers = True
        self.local_user.save()

        with patch('bookwyrm.broadcast.broadcast_task.delay'):
            incoming.handle_follow(activity)

        # notification created
        notification = models.Notification.objects.get()
        self.assertEqual(notification.user, self.local_user)
        self.assertEqual(notification.notification_type, 'FOLLOW_REQUEST')

        # the request should exist
        request = models.UserFollowRequest.objects.get()
        self.assertEqual(request.user_subject, self.remote_user)
        self.assertEqual(request.user_object, self.local_user)

        # the follow relationship should not exist
        follow = models.UserFollows.objects.all()
        self.assertEqual(list(follow), [])


    def test_handle_unfollow(self):
        ''' remove a relationship '''
        activity = {
            "type": "Undo",
            "@context": "https://www.w3.org/ns/activitystreams",
            "object": {
                "id": "https://example.com/users/rat/follows/123",
                "type": "Follow",
                "actor": "https://example.com/users/rat",
                "object": "http://local.com/user/mouse"
            }
        }
        models.UserFollows.objects.create(
            user_subject=self.remote_user, user_object=self.local_user)
        self.assertEqual(self.remote_user, self.local_user.followers.first())

        incoming.handle_unfollow(activity)
        self.assertIsNone(self.local_user.followers.first())


    def test_handle_follow_accept(self):
        ''' a remote user approved a follow request from local '''
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.com/users/rat/follows/123#accepts",
            "type": "Accept",
            "actor": "https://example.com/users/rat",
            "object": {
                "id": "https://example.com/users/rat/follows/123",
                "type": "Follow",
                "actor": "http://local.com/user/mouse",
                "object": "https://example.com/users/rat"
            }
        }

        models.UserFollowRequest.objects.create(
            user_subject=self.local_user,
            user_object=self.remote_user
        )
        self.assertEqual(models.UserFollowRequest.objects.count(), 1)

        incoming.handle_follow_accept(activity)

        # request should be deleted
        self.assertEqual(models.UserFollowRequest.objects.count(), 0)

        # relationship should be created
        follows = self.remote_user.followers
        self.assertEqual(follows.count(), 1)
        self.assertEqual(follows.first(), self.local_user)


    def test_handle_follow_reject(self):
        ''' turn down a follow request '''
        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "id": "https://example.com/users/rat/follows/123#accepts",
            "type": "Reject",
            "actor": "https://example.com/users/rat",
            "object": {
                "id": "https://example.com/users/rat/follows/123",
                "type": "Follow",
                "actor": "http://local.com/user/mouse",
                "object": "https://example.com/users/rat"
            }
        }

        models.UserFollowRequest.objects.create(
            user_subject=self.local_user,
            user_object=self.remote_user
        )
        self.assertEqual(models.UserFollowRequest.objects.count(), 1)

        incoming.handle_follow_reject(activity)

        # request should be deleted
        self.assertEqual(models.UserFollowRequest.objects.count(), 0)

        # relationship should be created
        follows = self.remote_user.followers
        self.assertEqual(follows.count(), 0)


    def test_handle_favorite(self):
        ''' fav a status '''
        activity = {
            '@context': 'https://www.w3.org/ns/activitystreams',
            'id': 'http://example.com/fav/1',
            'actor': 'https://example.com/users/rat',
            'published': 'Mon, 25 May 2020 19:31:20 GMT',
            'object': 'http://local.com/status/1',
        }

        incoming.handle_favorite(activity)

        fav = models.Favorite.objects.get(remote_id='http://example.com/fav/1')
        self.assertEqual(fav.status, self.status)
        self.assertEqual(fav.remote_id, 'http://example.com/fav/1')
        self.assertEqual(fav.user, self.remote_user)
