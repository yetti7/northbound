from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.urls import reverse

from . import test_personal_tbr_registration as registration_fixture
from . import test_staff_challenge_tbr as staff_fixture
from .forms import PersonalTbrRegistrationBookFormSet
from .models import (
    BookSubmission, ChallengeMonth, ChallengeSignupQuestion, HardcoverConnection, HardcoverSyncOutbox,
    ModifierProvenance, PersonalTBRMatch,
    Membership, MonthEnrollment, PersonalTBR, ReadingGroup, UserProfile,
)
from .permissions import can_manage_challenge_tbr
from .personal_tbr import confirm_personal_tbr
from .personal_tbr_matching import synchronize_submission
from .reader_hardcover import ReaderHardcoverUnavailable


class PlatformOwnerTbrAccessTests(TestCase):
    setUp = staff_fixture.StaffChallengeTbrTests.setUp
    books = staff_fixture.StaffChallengeTbrTests.books
    payload = staff_fixture.StaffChallengeTbrTests.payload

    def root(self):
        root = get_user_model().objects.create_superuser('platform-tbr-owner','root@example.com','password')
        UserProfile.objects.create(user=root)
        self.client.force_login(root)
        return root

    def test_global_access_without_membership_in_multiple_groups_or_staff_assignments(self):
        root = self.root()
        other_group = ReadingGroup.objects.create(name='Other Group',slug='other-tbr-group')
        other_reader = Membership.objects.create(group=other_group,user=self.reader_user,display_name='Other reader')
        other_month = ChallengeMonth.objects.create(group=other_group,name='Other challenge',starts_on=date(2026,10,1),ends_on=date(2026,10,31),tbr_enabled=True)
        other_enrollment = MonthEnrollment.objects.create(month=other_month,participant=other_reader)
        self.assertFalse(Membership.objects.filter(user=root).exists())
        for enrollment in (self.enrollment,other_enrollment):
            month=enrollment.month;args=[month.group.slug,month.pk,enrollment.participant.pk]
            self.assertTrue(can_manage_challenge_tbr(root,enrollment))
            self.assertContains(self.client.get(reverse('month-participant-list',args=args[:2])),'Manage TBR')
            self.assertContains(self.client.get(reverse('challenge-participant-detail',args=args)),'Manage TBR')
            self.assertContains(self.client.get(reverse('staff-challenge-tbr',args=args)),'Manage TBR')
        response=self.client.post(self.url,self.payload(self.books(2)))
        self.assertEqual(response.status_code,302)
        tbr=PersonalTBR.objects.get(enrollment=self.enrollment)
        self.assertEqual(tbr.books.count(),2)
        self.assertIsNone(tbr.confirmed_at)
        self.assertFalse(HardcoverSyncOutbox.objects.exists())
        self.assertFalse(can_manage_challenge_tbr(AnonymousUser(),self.enrollment))

    def test_root_search_uses_actor_then_group_never_reader_credentials(self):
        root=self.root()
        with patch('core.views.get_reader_hardcover_token',return_value='root-token') as token, patch('core.views.search_books',return_value=([],False)) as search:
            self.assertEqual(self.client.post(self.catalog_url,{'action':'search','query':'Book'}).status_code,200)
            token.assert_called_once_with(root)
            search.assert_called_once_with('root-token','Book')
        HardcoverConnection.objects.create(group=self.group,is_valid=True,encrypted_token='group-encrypted')
        with patch('core.views.get_reader_hardcover_token',side_effect=ReaderHardcoverUnavailable()) as token, patch('core.views.decrypt_token',return_value='group-token'), patch('core.views.search_books',return_value=([],False)) as search:
            self.assertEqual(self.client.post(self.catalog_url,{'action':'search','query':'Book'}).status_code,200)
            token.assert_called_once_with(root)
            search.assert_called_once_with('group-token','Book')
        self.assertFalse(HardcoverSyncOutbox.objects.exists())

    def test_root_cannot_bypass_staff_completion_requirement(self):
        self.root()
        response=self.client.post(self.url,self.payload(self.books(8),'complete'))
        self.assertContains(response,'requires nine valid book selections')
        self.assertFalse(PersonalTBR.objects.exists())
        self.assertEqual(response.status_code,200)


    def test_root_corrections_preserve_matched_books_and_awards(self):
        tbr=staff_fixture.StaffChallengeTbrTests.save(self,self.books(2))
        confirm_personal_tbr(enrollment=self.enrollment)
        book=tbr.books.first()
        submission=BookSubmission.objects.create(month=self.month,participant=self.reader,
            title=book.title_snapshot,author=book.author_snapshot,book_format=BookSubmission.Format.EBOOK,
            completed_on=date(2026,9,10),submitted_pages=100,approved_pages=100,status=BookSubmission.Status.APPROVED)
        synchronize_submission(submission)
        models=[BookSubmission,PersonalTBRMatch,ModifierProvenance]
        before=[list(model.objects.values()) for model in models]
        books_before=list(tbr.books.values())
        self.root()
        response=self.client.post(self.url,self.payload([self.books(2)[1]]))
        self.assertContains(response,'scoring history')
        self.assertEqual(before,[list(model.objects.values()) for model in models])
        self.assertEqual(books_before,list(tbr.books.values()))


class TbrRegistrationRemovalTests(TestCase):
    setUp = registration_fixture.PersonalTbrRegistrationTests.setUp
    payload = registration_fixture.PersonalTbrRegistrationTests.payload
    stage = registration_fixture.PersonalTbrRegistrationTests.stage
    finalize = registration_fixture.PersonalTbrRegistrationTests.finalize

    def selections(self):
        return [{'title':'Retain first','author':'Writer'},{'title':'Remove middle','author':'Writer'},{'title':'Retain last','author':'Writer'}]

    def removal_payload(self):
        data=self.payload(self.selections())
        data['tbr-books-1-DELETE']='on'
        # Native HTML submits the active rows in their updated order.
        data['tbr-books-2-ORDER']='1'
        data['tbr-books-0-ORDER']='2'
        return data

    def test_remove_control_submits_checkbox_state_not_hidden_text_value(self):
        formset=PersonalTbrRegistrationBookFormSet(prefix='tbr-books')
        widget=formset.forms[0].fields['DELETE'].widget
        self.assertEqual(widget.input_type,'checkbox')
        self.assertTrue(widget.attrs['hidden'])

    def test_removal_persists_through_review_confirmation_and_positions(self):
        self.stage(self.selections())
        response=self.client.post(self.register_url,self.removal_payload())
        self.assertNotContains(response,'Remove middle')
        self.assertContains(response,'2 / 9 Personal TBR books')
        self.assertContains(response,'not be eligible for the full 9-book completion bonus')
        response=self.finalize()
        self.assertEqual(response.status_code,302)
        tbr=PersonalTBR.objects.get()
        self.assertEqual(list(tbr.books.values_list('title_snapshot','position')),[('Retain last',1),('Retain first',2)])
        self.assertIsNotNone(tbr.confirmed_at)

    def test_deleted_row_stays_deleted_when_returning_from_summary(self):
        self.client.post(self.register_url,self.removal_payload())
        response=self.client.get(self.register_url)
        formset=response.context['tbr_formset']
        self.assertTrue(formset.forms[1]['DELETE'].value())
        self.assertIn('hidden',str(formset.forms[1]['DELETE']))
        self.assertEqual([book['title_snapshot'] for book in formset.book_values()],['Retain last','Retain first'])
        self.finalize()
        self.assertFalse(PersonalTBR.objects.get().books.filter(title_snapshot='Remove middle').exists())

    def test_invalid_edit_cannot_confirm_an_older_successful_summary(self):
        self.stage(self.selections())
        data=self.removal_payload();data['tbr-books-0-author_snapshot']=''
        response=self.client.post(self.register_url,data)
        self.assertContains(response,'Enter an author or remove this book.')
        response=self.client.post(self.confirm_url,follow=True)
        self.assertContains(response,'registration draft is unavailable or expired')
        self.assertFalse(MonthEnrollment.objects.exists())
        self.assertFalse(PersonalTBR.objects.exists())

    def test_invalid_registration_answers_also_invalidate_old_confirmation(self):
        ChallengeSignupQuestion.objects.create(month=self.month,wording='Required answer',question_type=ChallengeSignupQuestion.QuestionType.SHORT_TEXT,is_required=True,position=1)
        question=self.month.signup_questions.get()
        field=f'question_{question.pk}'
        self.client.post(self.register_url,{**self.payload(self.selections()),field:'Answered'})
        self.assertTrue(any(key.startswith('challenge-registration-draft:') for key in self.client.session.keys()))
        self.client.post(self.register_url,self.removal_payload())
        self.client.post(self.confirm_url)
        self.assertFalse(MonthEnrollment.objects.exists())

    def test_removed_invalid_book_is_ignored_but_remaining_invalid_books_are_not(self):
        data=self.removal_payload();data['tbr-books-1-cover_url_snapshot']='invalid URL'
        response=self.client.post(self.register_url,data)
        self.assertContains(response,'Confirm Registration')
        self.finalize()
        self.assertEqual(PersonalTBR.objects.get().books.count(),2)

    def test_confirmed_tbr_cannot_be_changed_by_re_registration_removal(self):
        self.stage(self.selections());self.finalize()
        enrollment=MonthEnrollment.objects.get();enrollment.is_active=False;enrollment.save()
        before=list(PersonalTBR.objects.get().books.values())
        response=self.client.post(self.register_url,self.removal_payload())
        self.assertContains(response,'previously confirmed Personal TBR will remain locked')
        self.finalize()
        self.assertEqual(before,list(PersonalTBR.objects.get().books.values()))

    def test_removing_all_books_confirms_empty_list_with_explicit_bonus_warning(self):
        data=self.payload(self.selections())
        for i in range(3):data[f'tbr-books-{i}-DELETE']='on'
        response=self.client.post(self.register_url,data)
        self.assertContains(response,'0 / 9 Personal TBR books')
        self.assertContains(response,'not be eligible for Personal TBR bonus pages')
        self.finalize()
        self.assertEqual(PersonalTBR.objects.get().books.count(),0)
