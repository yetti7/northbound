from django.test import TestCase
from django.urls import reverse
from . import test_branding as fixture
from .models import ChallengeMonth


class ChallengeBrandingLayoutTests(TestCase):
    setUp=fixture.BrandingTests.setUp
    setUpFixture=fixture.BrandingTests.setUpFixture
    make_book=fixture.BrandingTests.make_book
    save=fixture.BrandingTests.save

    def detail(self):
        return self.client.get(reverse('month-detail',args=[self.group.slug,self.month.pk]))

    def listing(self):
        return self.client.get(reverse('month-list',args=[self.group.slug]))

    def test_unbranded_and_group_inheritance_without_duplicate_captions(self):
        self.client.force_login(self.reader)
        empty=self.detail();self.assertContains(empty,'challenge-identity-main');self.assertNotContains(empty,'challenge-identity-logo')
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        response=self.detail();self.assertContains(response,self.group.logo.url,count=1);self.assertNotContains(response,self.group.banner.url)
        self.assertNotContains(response,'community-logos');self.assertContains(response,'<h1>'+self.month.name+'</h1>',html=True)
        self.assertContains(self.listing(),'challenge-card-logo')

    def test_challenge_art_takes_precedence_per_asset_and_mode_is_inherited(self):
        self.save(logo=fixture.upload(),banner=fixture.upload());self.group.refresh_from_db()
        self.save(target=self.month,logo=fixture.upload());self.month.refresh_from_db()
        self.client.force_login(self.reader)
        for response in [self.detail(),self.listing()]:
            self.assertContains(response,self.month.logo.url);self.assertNotContains(response,self.group.logo.url);self.assertNotContains(response,self.group.banner.url)
        self.group.branding_mode='banner';self.group.save(update_fields=['branding_mode'])
        self.assertContains(self.detail(),self.group.banner.url)
        self.save(target=self.month,banner=fixture.upload());self.month.refresh_from_db()
        for response in [self.detail(),self.listing()]:
            self.assertContains(response,self.month.banner.url);self.assertNotContains(response,self.group.banner.url)

    def test_active_score_visibility_and_completed_statistics_are_preserved(self):
        for user,visible in [(self.reader,False),(self.admin,True)]:
            self.client.force_login(user);response=self.listing()
            self.assertEqual('verified pages' in response.content.decode(),visible)
            if not visible:self.assertNotContains(response,'978421')
        ChallengeMonth.objects.filter(pk=self.month.pk).update(status='completed')
        self.client.force_login(self.reader);response=self.listing();self.assertContains(response,'978421');self.assertContains(response,'View Wrapped')

    def test_long_names_controls_and_group_layout_are_preserved(self):
        self.month.name='November Challenge with xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'[:80]
        self.month.save(update_fields=['name']);self.save(target=self.month,logo=fixture.upload())
        self.client.force_login(self.admin);response=self.detail()
        self.assertContains(response,self.month.name);self.assertContains(response,'Challenge Settings');self.assertContains(response,'challenge-identity-actions')
        group=self.client.get(reverse('group-detail',args=[self.group.slug]))
        self.assertContains(group,'group-identity-main');self.assertNotContains(group,'css/challenge_identity.css')

    def test_card_artwork_does_not_leak_between_loop_iterations(self):
        self.save(target=self.month,logo=fixture.upload());self.month.refresh_from_db()
        ChallengeMonth.objects.create(group=self.group,name='Unbranded sibling',status='active',starts_on=self.month.starts_on,ends_on=self.month.ends_on)
        self.client.force_login(self.reader)
        response=self.listing()
        self.assertContains(response,self.month.logo.url,count=1)
        self.assertContains(response,'Unbranded sibling')
