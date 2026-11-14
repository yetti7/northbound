"""Inert Bingo/Scavenger foundations. No approval or scoring hooks in Phase 1."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class PromptCategory(models.Model):
    class Kind(models.TextChoices):
        BINGO = 'bingo', 'Bingo'
        SCAVENGER = 'scavenger', 'Scavenger Hunt'

    month = models.ForeignKey('ChallengeMonth', on_delete=models.PROTECT, related_name='prompt_categories')
    kind = models.CharField(max_length=12, choices=Kind.choices)
    enabled = models.BooleanField(default=False)
    completion_bonus = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(2147483647)])
    frozen_prompt_set = models.JSONField(default=list, blank=True, editable=False)
    structure_frozen_at = models.DateTimeField(null=True, blank=True, editable=False)
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['month','kind'], name='unique_challenge_prompt_category'),
                       models.CheckConstraint(condition=Q(kind__in=['bingo','scavenger']), name='valid_prompt_category_kind')]

    @property
    def structure_locked(self):
        return bool(self.structure_frozen_at or self.claims.exists() or self.completions.exists())

    def freeze_structure(self):
        if not self.structure_frozen_at:
            self.frozen_prompt_set = list(self.prompts.filter(is_active=True).order_by('number').values('id','number'))
            if not self.frozen_prompt_set:
                raise ValidationError('Configure required prompts before recording activity.')
            self.structure_frozen_at = timezone.now()
            self.save(update_fields=['frozen_prompt_set','structure_frozen_at'])

    def clean(self):
        from .prompt_configuration import automation_excluded
        if self.enabled and self.month_id and automation_excluded(self.month):
            raise ValidationError('This Challenge is excluded from automated prompts.')

    def save(self, *args, **kwargs):
        if self.pk:
            old = type(self).objects.get(pk=self.pk)
            if (old.month_id,old.kind) != (self.month_id,self.kind):
                raise ValidationError('Prompt category identity cannot be changed.')
            if old.structure_frozen_at and (old.frozen_prompt_set != self.frozen_prompt_set or old.structure_frozen_at != self.structure_frozen_at):
                raise ValidationError('The frozen required prompt set must be preserved.')
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Retain category history; disable the category instead.')


class ChallengePrompt(models.Model):
    class SubmissionType(models.TextChoices):
        BOOK = 'book', 'Book'
        PHOTO = 'photo', 'Photo'

    submission_type = models.CharField(max_length=5, choices=SubmissionType.choices, default=SubmissionType.BOOK)
    category = models.ForeignKey(PromptCategory, on_delete=models.PROTECT, related_name='prompts')
    number = models.PositiveSmallIntegerField(validators=[MaxValueValidator(30)])
    description = models.TextField()
    points = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(2147483647)])
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta:
        ordering = ['number','pk']
        constraints = [models.UniqueConstraint(fields=['category','number'], name='unique_number_per_prompt_category'),
            models.CheckConstraint(condition=Q(number__gte=1,number__lte=30), name='prompt_number_one_to_thirty')]

    def clean(self):
        if not (self.description or '').strip():
            raise ValidationError({'description':'Enter the full prompt description.'})
        if self.category_id:
            if self.category.kind == 'bingo' and self.submission_type != 'book':
                raise ValidationError({'submission_type': 'Bingo prompts must be Book prompts.'})
            old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
            if old and old.category_id != self.category_id:
                raise ValidationError('Prompt category cannot be changed.')
            if self.category.structure_locked and (old is None or (old.number,old.is_active,old.submission_type)!=(self.number,self.is_active,self.submission_type)):
                raise ValidationError('The required prompt set is frozen. Only wording and point values may be edited; existing evidence is preserved.')

    def save(self, *args, **kwargs):
        with transaction.atomic():
            self.category = PromptCategory.objects.select_for_update().get(pk=self.category_id)
            old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
            self.full_clean()
            if old and any(getattr(old,f)!=getattr(self,f) for f in ('number','description','points','is_active','submission_type')):
                self.revision = old.revision + 1
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = set(kwargs['update_fields']) | {'revision'}
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Retire prompts instead of deleting their history.')


class PromptClaim(models.Model):
    class State(models.TextChoices):
        RESERVED='reserved','Reserved'
        QUALIFIED='qualified','Qualified'
        RELEASED='released','Released'
        REJECTED='rejected','Rejected'

    category = models.ForeignKey(PromptCategory, on_delete=models.PROTECT, related_name='claims')
    prompt = models.ForeignKey(ChallengePrompt, on_delete=models.PROTECT, related_name='claims')
    submission = models.ForeignKey('BookSubmission', null=True, blank=True, on_delete=models.PROTECT, related_name='prompt_claims')
    photo_submission = models.OneToOneField('ScavengerPhotoSubmission', null=True, blank=True, on_delete=models.PROTECT, related_name='prompt_claim')
    participant = models.ForeignKey('Membership', on_delete=models.PROTECT, related_name='prompt_claims')
    state = models.CharField(max_length=12, choices=State.choices, default=State.RESERVED)
    is_current = models.BooleanField(default=True)
    selection_evidence = models.JSONField(default=dict, blank=True, editable=False)
    earning_evidence = models.JSONField(default=dict, blank=True, editable=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_prompt_claims')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(submission__isnull=False, photo_submission__isnull=True) | Q(submission__isnull=True, photo_submission__isnull=False), name='prompt_claim_exactly_one_target'),
            models.UniqueConstraint(fields=['submission','category'],condition=Q(is_current=True),name='one_current_prompt_per_submission_category'),
            models.UniqueConstraint(fields=['participant','prompt'],condition=Q(is_current=True,state__in=['reserved','qualified']),name='one_holding_prompt_claim_per_reader'),
            models.CheckConstraint(condition=Q(state__in=['reserved','qualified','released','rejected']),name='valid_prompt_claim_state'),
        ]

    def clean(self):
        if bool(self.submission_id) == bool(self.photo_submission_id):
            raise ValidationError('A prompt claim requires exactly one Book or Photo target.')
        if self.photo_submission_id:
            photo = self.photo_submission
            if self.category.kind != 'scavenger' or self.prompt.submission_type != 'photo':
                raise ValidationError('Photo claims require a Scavenger Hunt Photo prompt.')
            if (photo.month_id, photo.participant_id) != (self.category.month_id, self.participant_id):
                raise ValidationError('The Photo must belong to the claim Challenge and Reader.')
            credit_actor=getattr(self,'_photo_credit_actor',None)
            credit_action=getattr(self,'_photo_credit_action','')
            if credit_actor:
                from .photo_actions import can_change_photo_credit,can_correct_photo
                if not (can_correct_photo(credit_actor,photo) if credit_action=='correct' else can_change_photo_credit(credit_actor,photo,credit_action)):raise ValidationError('Current scoped Photo credit authority is required.')
            if self.state == 'qualified' and not credit_actor:
                actor = getattr(self, '_photo_review_actor', None)
                if (photo.status != 'approved' or not photo.reviewer_verified or not self.earning_evidence
                        or not self.reviewed_by_id or not self.reviewed_at or actor is None
                        or self.reviewed_by_id != actor.pk or photo.reviewed_by_id != actor.pk):
                    raise ValidationError('Photo qualification requires the verified review service.')
                from .photo_submissions import can_review_photo
                if not can_review_photo(actor, photo):
                    raise ValidationError('Current scoped Photo review authority is required.')
            elif self.earning_evidence and not credit_actor:
                raise ValidationError('Earned Photo changes require the credit workflow.')
            expected = 'qualified' if photo.status == 'approved' else 'reserved' if photo.status == 'pending' else 'rejected' if photo.status == 'rejected' else 'released'
            if self.state != expected:
                raise ValidationError('The Photo claim must match its activity state.')
        elif self.submission_id and self.prompt.submission_type != 'book':
            raise ValidationError('Book submissions require Book prompts.')
        if self.prompt_id and self.category_id and self.prompt.category_id != self.category_id:
            raise ValidationError('The prompt must belong to this category.')
        if self.submission_id and self.category_id and self.submission.month_id != self.category.month_id:
            raise ValidationError('The submission must belong to the category Challenge.')
        if self.submission_id and self.participant_id and self.submission.participant_id != self.participant_id:
            raise ValidationError('The claim must belong to the submission Reader.')
        if self.participant_id and self.category_id and self.participant.group_id != self.category.month.group_id:
            raise ValidationError('The Reader must belong to the category Group.')
        if self.pk:
            old=type(self).objects.get(pk=self.pk)
            correction_actor=getattr(self,'_photo_approved_correction_actor',None)
            if correction_actor:
                from .photo_actions import validate_approved_claim_correction
                validate_approved_claim_correction(self,old,correction_actor)
            elif old.earning_evidence and old.earning_evidence!=self.earning_evidence:
                raise ValidationError('Frozen prompt earning evidence must be preserved.')
            identity_fields = ('category_id','submission_id','photo_submission_id','participant_id')
            if any(getattr(old,f)!=getattr(self,f) for f in identity_fields):
                raise ValidationError('Preserve claim ownership and target history.')
            if (old.prompt_id != self.prompt_id or old.selection_evidence != self.selection_evidence) and not correction_actor:
                actor = getattr(self, '_photo_selection_edit_actor', None)
                if not self.photo_submission_id or actor is None or old.earning_evidence or old.state != 'reserved' or not old.is_current:
                    raise ValidationError('Preserve selection history; use the authorized correction workflow.')
                from .photo_submissions import validate_pending_photo_selection_edit
                validate_pending_photo_selection_edit(self, actor)

    def save(self,*args,**kwargs):
        from .prompt_reservations import require_ready
        require_ready()
        with transaction.atomic():
            from .models import BookSubmission
            if self.submission_id:
                BookSubmission.objects.select_for_update().get(pk=self.submission_id)
            elif self.photo_submission_id:
                ScavengerPhotoSubmission.objects.select_for_update().get(pk=self.photo_submission_id)
            self.category=PromptCategory.objects.select_for_update().get(pk=self.category_id)
            if self.is_current and self.state in {'reserved','qualified'} and self.category.kind=='bingo' and self.submission_id:
                from .prompt_reservations import assert_bingo_allowed
                assert_bingo_allowed(self.submission)
            if not self.pk and not self.selection_evidence:
                self.selection_evidence={'prompt_id':self.prompt_id,'number':self.prompt.number,'description':self.prompt.description,'revision':self.prompt.revision,'points':self.prompt.points}
            self.full_clean()
            # Any recorded activity freezes structure permanently, even after later release.
            self.category.freeze_structure()
            return super().save(*args,**kwargs)

    def delete(self,*args,**kwargs):
        raise ValidationError('Preserve prompt claim history.')


class PromptCompletion(models.Model):
    category = models.ForeignKey(PromptCategory,on_delete=models.PROTECT,related_name='completions')
    participant = models.ForeignKey('Membership',on_delete=models.PROTECT,related_name='prompt_completions')
    frozen_prompt_set = models.JSONField(default=list,blank=True)
    bonus_amount_snapshot = models.PositiveIntegerField(default=0,validators=[MaxValueValidator(2147483647)])
    is_active = models.BooleanField(default=False)
    qualified_at = models.DateTimeField(null=True,blank=True)
    effective_date = models.DateField(null=True,blank=True)
    evidence = models.JSONField(default=dict,blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['category','participant'],name='one_prompt_completion_per_reader_category')]

    def clean(self):
        if self.participant_id and self.category_id and self.participant.group_id != self.category.month.group_id:
            raise ValidationError('The completion Reader must belong to the category Group.')
        if self.is_active and (not self.frozen_prompt_set or not self.qualified_at or not self.effective_date):
            raise ValidationError('Active completion requires a frozen prompt set and earning dates.')
        if self.frozen_prompt_set and self.frozen_prompt_set != self.category.frozen_prompt_set:
            raise ValidationError('Completion must preserve the frozen required prompt set.')
        if self.pk:
            old=type(self).objects.get(pk=self.pk)
            if any(getattr(old,f)!=getattr(self,f) for f in ('category_id','participant_id','frozen_prompt_set','bonus_amount_snapshot','qualified_at','effective_date','evidence')):
                raise ValidationError('Completion earning evidence must be preserved.')

    def save(self,*args,**kwargs):
        if self.is_active:
            from .prompt_reservations import require_ready
            from .prompt_configuration import automation_excluded
            require_ready()
            if automation_excluded(self.category.month):raise ValidationError('This Challenge is excluded from prompt completion.')
        self.full_clean()
        return super().save(*args,**kwargs)

    def delete(self,*args,**kwargs):
        raise ValidationError('Preserve completion history.')


class ScavengerPhotoSubmission(models.Model):
    """Standalone attestation only. No uploads, review execution or scoring in Phase A."""
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        WITHDRAWN = 'withdrawn', 'Withdrawn'
        VOIDED = 'voided', 'Voided'

    month = models.ForeignKey('ChallengeMonth', on_delete=models.PROTECT, related_name='photo_submissions')
    participant = models.ForeignKey('Membership', on_delete=models.PROTECT, related_name='photo_submissions')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    discord_message_url = models.URLField(max_length=500, blank=True)
    reader_confirmed = models.BooleanField(default=False, editable=False)
    submitted_at = models.DateTimeField(default=timezone.now, editable=False)
    reviewer_verified = models.BooleanField(default=False, editable=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='reviewed_scavenger_photos')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='removed_scavenger_photos')
    removal_reason = models.TextField(blank=True)
    version = models.PositiveIntegerField(default=1, editable=False)
    participation_period = models.ForeignKey('ParticipationPeriod', on_delete=models.PROTECT, related_name='photo_submissions')
    accepted_team_assignment = models.ForeignKey('TeamAssignment', null=True, blank=True, on_delete=models.PROTECT, related_name='photo_submissions')
    acceptance_evidence = models.JSONField(default=dict, blank=True, editable=False)

    class Meta:
        ordering = ['-submitted_at', '-pk']
        constraints = [
            models.CheckConstraint(condition=Q(reader_confirmed=True), name='photo_reader_attested'),
            models.CheckConstraint(condition=Q(status__in=['pending','approved','rejected','withdrawn','voided']), name='photo_valid_status'),
            models.CheckConstraint(condition=~Q(status='approved') | Q(reviewer_verified=True, reviewed_by__isnull=False, reviewed_at__isnull=False), name='photo_approval_verified'),
            models.CheckConstraint(condition=Q(status__in=['withdrawn','voided'], removed_at__isnull=False) | Q(status__in=['pending','approved','rejected'], removed_at__isnull=True), name='photo_removal_state'),
        ]

    def clean(self):
        from .photo_validation import validate_discord_message_url
        validate_discord_message_url(self.discord_message_url)
        if not self.reader_confirmed:
            raise ValidationError({'reader_confirmed': 'Confirm that you posted your photo in the designated Discord channel.'})
        if not self.month_id or not self.participant_id:
            raise ValidationError('Challenge and Reader are required.')
        if self.participant.group_id != self.month.group_id:
            raise ValidationError('The Reader must belong to the Challenge Group.')
        if self.status == 'approved' and not (self.reviewer_verified and self.reviewed_by_id and self.reviewed_at):
            raise ValidationError('Photo approval requires explicit reviewer verification and attribution.')
        if self.status == 'rejected' and self.reviewer_verified:
            raise ValidationError('Rejected Photos must not record approval verification.')
        if not self.participation_period_id or not self.acceptance_evidence:
            raise ValidationError('Captured participation evidence is required.')
        period = self.participation_period
        if (period.enrollment.month_id, period.enrollment.participant_id) != (self.month_id, self.participant_id) or not period.includes_acceptance(self.submitted_at):
            raise ValidationError('Photo acceptance must belong to its Reader participation period.')
        assignment = self.accepted_team_assignment
        if assignment and ((assignment.month_id, assignment.participant_id) != (self.month_id, self.participant_id)
                or not assignment.assigned_at or assignment.assigned_at > self.submitted_at or assignment.ended_at and assignment.ended_at <= self.submitted_at):
            raise ValidationError('Captured Team assignment does not contain Photo acceptance.')
        expected = {'enrollment_id': period.enrollment_id, 'period_id': period.pk,
                    'team_assignment_id': assignment.pk if assignment else None,
                    'team_id': assignment.team_id if assignment else None}
        if (any(self.acceptance_evidence.get(k) != v for k, v in expected.items())
                or self.acceptance_evidence.get('accepted_at') != self.submitted_at.isoformat()
                or self.acceptance_evidence.get('reader_confirmed') is not True):
            raise ValidationError('Captured participation or Team evidence is inconsistent.')

    def save(self, *args, **kwargs):
        from .prompt_reservations import require_ready, _reader_eligible, category_available
        require_ready()
        with transaction.atomic():
            from .models import ChallengeMonth
            self.month = ChallengeMonth.objects.select_for_update().select_related('group').get(pk=self.month_id)
            old = type(self).objects.select_for_update().filter(pk=self.pk).first() if self.pk else None
            if old:
                for field in ('month_id','participant_id','reader_confirmed','submitted_at','participation_period_id','accepted_team_assignment_id','acceptance_evidence'):
                    if getattr(old, field) != getattr(self, field):
                        raise ValidationError('Photo ownership, attestation and acceptance evidence are immutable.')
                if self.version != old.version:
                    raise ValidationError('This Photo activity changed. Reload before continuing.')
                from .photo_actions import can_correct_photo,can_change_photo_credit
                correction_actor=getattr(self,'_photo_correction_actor',None)
                link_correction=bool(correction_actor and can_correct_photo(correction_actor,self)
                    and all(getattr(old,f.attname)==getattr(self,f.attname) for f in self._meta.concrete_fields if f.name not in {'discord_message_url','version'}))
                credit_actor=getattr(self,'_photo_credit_actor',None)
                credit_action=getattr(self,'_photo_credit_action','')
                credit_change=bool(credit_actor and can_change_photo_credit(credit_actor,self,credit_action)
                    and (old.status,self.status) in {('approved','voided'),('voided','approved')}
                    and all(getattr(old,key)==getattr(self,key) for key in ('discord_message_url','reviewer_verified','reviewed_by_id','reviewed_at')))
                if (correction_actor and not link_correction) or (credit_actor and not credit_change):raise ValidationError('Invalid Photo correction or credit transition.')
                review_change = not (link_correction or credit_change) and (any(getattr(old, key) != getattr(self, key) for key in ('reviewer_verified','reviewed_by_id','reviewed_at')) or self.status == 'approved' or (self.status == 'rejected' and old.status != 'rejected'))
                if review_change:
                    actor = getattr(self, '_photo_review_actor', None)
                    from .photo_submissions import can_review_photo
                    if (actor is None or old.status != 'pending' or self.status not in {'approved','rejected'}
                            or self.reviewed_by_id != actor.pk or not self.reviewed_at or not can_review_photo(actor,self)):
                        raise ValidationError('Photo decisions require the authorized review service.')
                if old.status != self.status and not credit_change and not (old.status == 'pending' or self.status == 'pending'):
                    raise ValidationError('Unsupported Photo state transition.')
                if old.status != 'pending' and old.status == self.status and self.discord_message_url != old.discord_message_url and not link_correction:
                    raise ValidationError('Only Pending Photo activities may be edited.')
                self.version += 1
                if kwargs.get('update_fields') is not None:
                    kwargs['update_fields'] = set(kwargs['update_fields']) | {'version'}
            elif self.status != 'pending':
                raise ValidationError('Photo submissions must start Pending.')
            if self.month.status not in {'active','finalizing'} or not category_available(self.month, 'scavenger'):
                raise ValidationError('Photo activity is unavailable in this Challenge stage or category.')
            if not old or self.status == 'pending' and not link_correction:
                _reader_eligible(self)
                if not old and self.month.status != 'active':
                    raise ValidationError('New Photo submissions require an Active Challenge.')
            self.full_clean()
            result = super().save(*args, **kwargs)
            if old and old.status != self.status and not getattr(self, '_photo_review_actor', None) and not getattr(self,'_photo_credit_actor',None):
                claim = PromptClaim.objects.select_for_update().filter(photo_submission=self).first()
                if claim:
                    if claim.earning_evidence or claim.state == 'qualified':
                        raise ValidationError('Earned Photo changes require a future scoring workflow.')
                    if self.status == 'pending':
                        from .photo_submissions import _prompt
                        _prompt(self.month, self.participant_id, claim.prompt_id, photo_id=self.pk)
                    claim.state = 'reserved' if self.status == 'pending' else 'rejected' if self.status == 'rejected' else 'released'
                    claim.save(update_fields=['state'])
            return result

    def delete(self, *args, **kwargs):
        raise ValidationError('Preserve Photo submission history.')
