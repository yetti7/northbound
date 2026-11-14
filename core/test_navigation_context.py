from urllib.parse import urlsplit,parse_qs
from django.test import RequestFactory,override_settings
from django.urls import reverse,resolve
from django.core import signing
from .test_navigation_foundation import NavigationFoundationTests
from .navigation_context import contextual_url, contextual_parent, SALT
from .models import ReadingGroup,ChallengeMonth,ChallengeStaffAssignment

@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ContextualNavigationTests(NavigationFoundationTests):
    def req(self,name,kwargs,query=None,user=None):
        q=RequestFactory().get(reverse(name,kwargs=kwargs),query or {})
        q.user=user or self.owner_user;q.resolver_match=resolve(q.path)
        return q
    def target(self):return reverse('submission-manage',kwargs=self.month_kwargs(pk=999))
    def test_review_origin_filters_propagate_through_edit_without_actions(self):
        source=self.req('review-queue',self.month_kwargs(),{'q':'long title','page':'3','sort':'title','next':'https://evil.example'})
        link=contextual_url(source,self.target());token=parse_qs(urlsplit(link).query)['nav'][0]
        page=self.req('submission-manage',self.month_kwargs(pk=999),{'nav':token})
        result=contextual_parent(page)
        self.assertIn('q=long+title',result['logical_parent_url']);self.assertIn('page=3',result['logical_parent_url']);self.assertNotIn('next=',result['logical_parent_url'])
        edit=contextual_url(page,reverse('submission-editor',kwargs=self.month_kwargs(pk=999)))
        self.assertEqual(parse_qs(urlsplit(edit).query)['nav'],[token])
        self.assertEqual(contextual_url(page,'https://evil.example'), 'https://evil.example')
        self.assertEqual(contextual_url(page,reverse('submission-remove',kwargs=self.month_kwargs(pk=999))),reverse('submission-remove',kwargs=self.month_kwargs(pk=999)))
    def test_team_books_preserves_view_page_and_search(self):
        source=self.req('team-detail',self.month_kwargs(pk=self.team.pk),{'books_page':'2','q':'book','sort':'reader','view':'roster'})
        link=contextual_url(source,reverse('team-book-detail',kwargs=self.month_kwargs(pk=self.team.pk,book_pk=999)))
        token=parse_qs(urlsplit(link).query)['nav'][0]
        result=contextual_parent(self.req('team-book-detail',self.month_kwargs(pk=self.team.pk,book_pk=999),{'nav':token}))
        self.assertIn('view=books',result['logical_parent_url']);self.assertIn('books_page=2',result['logical_parent_url'])
    def test_tampered_expired_other_user_and_cross_group_fall_back(self):
        source=self.req('review-queue',self.month_kwargs())
        token=parse_qs(urlsplit(contextual_url(source,self.target())).query)['nav'][0]
        for value,user in [(token+'x',self.owner_user),(token,self.reader_user)]:
            self.assertIsNone(contextual_parent(self.req('submission-manage',self.month_kwargs(pk=999),{'nav':value},user)))
        from unittest.mock import patch
        with patch('django.core.signing.time.time',return_value=0):old=signing.dumps({'user':self.owner_user.pk,'name':'review-queue','kwargs':self.month_kwargs(),'query':{}},salt=SALT)
        self.assertIsNone(contextual_parent(self.req('submission-manage',self.month_kwargs(pk=999),{'nav':old})))
        self.assertIsNone(contextual_parent(self.req('submission-manage',{'group_slug':'other','month_pk':self.month.pk,'pk':999},{'nav':token})))
        ChallengeStaffAssignment.objects.filter(month=self.month,membership=self.owner).delete()
        # Owner has independent queue oversight, so revoke Group membership too.
        self.owner.is_active=False;self.owner.save(update_fields=['is_active'])
        self.assertIsNone(contextual_parent(self.req('submission-manage',self.month_kwargs(pk=999),{'nav':token})))
    def test_direct_submission_fallback_and_no_untrusted_next(self):
        result=self.nav('submission-manage',self.month_kwargs(pk=999),query={'next':'https://evil.example'})
        self.assertEqual(result['logical_parent_url'],self.month.get_absolute_url())
        review=self.nav('submission-review',self.month_kwargs(pk=999))
        self.assertEqual(review['logical_parent_url'],reverse('review-queue',kwargs=self.month_kwargs()))
    def test_reader_history_origin(self):
        source=self.req('participant-detail',{'group_slug':self.group.slug,'pk':self.reader.pk},{'page':'2'})
        token=parse_qs(urlsplit(contextual_url(source,self.target())).query)['nav'][0]
        result=contextual_parent(self.req('submission-manage',self.month_kwargs(pk=999),{'nav':token}))
        self.assertEqual(result['logical_parent_label'],'Back to Reader History')
        self.assertIn('page=2',result['logical_parent_url'])

    def test_team_context_revoked_after_leader_assignment_ends(self):
        source=self.req('team-detail',self.month_kwargs(pk=self.team.pk),user=self.reader_user)
        token=parse_qs(urlsplit(contextual_url(source,self.target())).query)['nav'][0]
        # Current own-team Reader book visibility may persist; move the assignment out instead.
        from .models import TeamAssignment
        from django.utils import timezone
        self.leader_assignment.ended_at=timezone.now();self.leader_assignment.save()
        TeamAssignment.objects.filter(month=self.month,participant=self.reader).update(ended_at=timezone.now())
        self.assertIsNone(contextual_parent(self.req('submission-manage',self.month_kwargs(pk=999),{'nav':token},self.reader_user)))

    def test_rendered_manage_edit_and_correction_have_one_return_and_keep_cancel(self):
        from datetime import date
        from .models import BookSubmission,AuditEvent
        book=BookSubmission.objects.create(month=self.month,participant=self.reader,title='A long book title',author='Author',book_format='ebook',completed_on=date(2026,8,3),submitted_pages=100,approved_pages=100,status='approved')
        source=self.req('review-queue',self.month_kwargs(),{'page':'3','q':'book'})
        target=reverse('submission-manage',kwargs=self.month_kwargs(pk=book.pk))
        token=parse_qs(urlsplit(contextual_url(source,target)).query)['nav'][0]
        before=AuditEvent.objects.count()
        for route in ['submission-manage','submission-editor','submission-fix-book','submission-fix-pages','submission-credit']:
            response=self.client.get(reverse(route,kwargs=self.month_kwargs(pk=book.pk)),{'nav':token})
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'class="back-button contextual-back"',count=1)
            self.assertEqual(response.context['logical_parent_label'],'Back to Review Queue')
            if route=='submission-editor':
                self.assertEqual(response.context['cancel_url'],target)
        book.refresh_from_db();self.assertEqual((book.title,book.approved_pages,book.status),('A long book title',100,'approved'))
        self.assertEqual(AuditEvent.objects.count(),before)

    def test_activity_report_origin_is_reauthorized(self):
        source=self.req('submission-activity',self.month_kwargs(),{'page':'4'})
        token=parse_qs(urlsplit(contextual_url(source,self.target())).query)['nav'][0]
        result=contextual_parent(self.req('submission-manage',self.month_kwargs(pk=999),{'nav':token}))
        self.assertEqual(result['logical_parent_label'],'Back to Recent Activity')
        self.assertIn('page=4',result['logical_parent_url'])

class RecapContextTests(__import__('core.test_recap_navigation',fromlist=['RecapNavigationTests']).RecapNavigationTests):
    def test_filtered_chronicle_and_challenge_card_origins(self):
        from django.test import RequestFactory
        from .navigation_context import contextual_url,contextual_parent
        self.client.force_login(self.admin)
        query={'group':str(self.group.pk),'challenge':str(self.month.pk),'perspective':'reader','period':'all'}
        request=RequestFactory().get(reverse('recap-dashboard'),query);request.user=self.admin;request.resolver_match=resolve(request.path)
        target=reverse('recap-wrapped')+'?'+__import__('urllib.parse',fromlist=['urlencode']).urlencode(query)
        link=contextual_url(request,target);token=parse_qs(urlsplit(link).query)['nav'][0]
        response=self.client.get(link)
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['logical_parent_label'],'Back to Chronicle')
        self.assertIn('challenge='+str(self.month.pk),response.context['logical_parent_url'])
        self.assertContains(response,'class="back-button contextual-back"',count=1)
        from .templatetags.navigation import challenge_wrapped_url
        card=challenge_wrapped_url({'request':request},self.month,target)
        response=self.client.get(card)
        self.assertEqual(response.context['logical_parent_url'],self.month.get_absolute_url())
        self.assertEqual(response.context['logical_parent_label'],'Back to Challenge')
        # A valid signature never substitutes for current recap authorization.
        destination=RequestFactory().get(reverse('recap-wrapped'),{'nav':token});destination.user=self.admin;destination.resolver_match=resolve(destination.path)
        from unittest.mock import patch
        from django.core.exceptions import PermissionDenied
        with patch('core.recaps.build_recap',side_effect=PermissionDenied):
            self.assertIsNone(contextual_parent(destination))

class PromptContextTests(__import__('core.test_prompt_progress',fromlist=['PromptProgressTests']).PromptProgressTests):
    def test_scoped_report_return_and_revoked_reader_filter(self):
        from .navigation_context import contextual_url,contextual_parent
        request=RequestFactory().get(reverse('prompt-progress-report',args=[self.group.slug,self.month.pk]),{'reader':self.reader.pk,'category':'bingo','page':'2'})
        request.user=self.actor;request.resolver_match=resolve(request.path)
        target=reverse('submission-review',args=[self.group.slug,self.month.pk,self.book.pk])
        token=parse_qs(urlsplit(contextual_url(request,target)).query)['nav'][0]
        destination=RequestFactory().get(target,{'nav':token});destination.user=self.actor;destination.resolver_match=resolve(target)
        self.assertIn('category=bingo',contextual_parent(destination)['logical_parent_url'])
        self.assertIn('reader='+str(self.reader.pk),contextual_parent(destination)['logical_parent_url'])
        from unittest.mock import patch
        with patch('core.prompt_progress.reporting_scope',return_value=None):
            fresh=RequestFactory().get(target,{'nav':token});fresh.user=self.actor;fresh.resolver_match=resolve(target)
            self.assertIsNone(contextual_parent(fresh))

    def test_retired_report_category_context_falls_back(self):
        request=RequestFactory().get(reverse('prompt-progress-report',args=[self.group.slug,self.month.pk]),{'category':'bingo'})
        request.user=self.actor;request.resolver_match=resolve(request.path)
        target=reverse('submission-review',args=[self.group.slug,self.month.pk,self.book.pk])
        token=parse_qs(urlsplit(contextual_url(request,target)).query)['nav'][0]
        from .models import PromptCategory
        PromptCategory.objects.filter(month=self.month,kind='bingo').update(enabled=False)
        destination=RequestFactory().get(target,{'nav':token});destination.user=self.actor;destination.resolver_match=resolve(target)
        self.assertIsNone(contextual_parent(destination))
