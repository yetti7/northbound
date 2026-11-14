from django import forms
from django.core.exceptions import ValidationError
from .photo_submissions import available_photo_prompts
from .photo_validation import validate_discord_message_url


class PhotoSubmissionForm(forms.Form):
    prompt = forms.ModelChoiceField(queryset=None, label='Scavenger Hunt Prompt', empty_label='Choose a Photo prompt')
    discord_message_url = forms.URLField(required=False, max_length=500, label='Discord Message Link (Optional)',
        help_text='A direct Discord message link, if available. Photos stay in Discord.')
    reader_confirmed = forms.BooleanField(label='I have posted my photo in the designated Discord channel.')

    def __init__(self, *args, month, member, photo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['prompt'].queryset = available_photo_prompts(month, member.pk, photo_id=photo.pk if photo else None)
        self.fields['prompt'].label_from_instance = lambda p: f'{p.number}. {p.description}'
        if photo:
            self.fields.pop('reader_confirmed')
            self.fields['version'] = forms.IntegerField(min_value=1, widget=forms.HiddenInput)

    def clean_discord_message_url(self):
        value = self.cleaned_data.get('discord_message_url', '')
        try:
            validate_discord_message_url(value)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages) from None
        return value


class PhotoWithdrawalForm(forms.Form):
    version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class PhotoReviewForm(forms.Form):
    version = forms.CharField(widget=forms.HiddenInput)
    decision = forms.ChoiceField(choices=[('approved','Approve'),('rejected','Reject')], widget=forms.HiddenInput)
    reviewer_verified = forms.BooleanField(required=False, label='I have verified this photo in the designated Discord channel.')

    def clean(self):
        data = super().clean()
        if data.get('decision') == 'approved' and not data.get('reviewer_verified'):
            self.add_error('reviewer_verified','Confirm that you verified the photo before approval.')
        return data


class PhotoStaffCorrectionForm(forms.Form):
    preview_token=forms.CharField(required=False,widget=forms.HiddenInput)
    version=forms.CharField(widget=forms.HiddenInput)
    prompt=forms.ModelChoiceField(queryset=None,label='Photo prompt')
    discord_message_url=forms.URLField(required=False,max_length=500,label='Discord Message Link (Optional)')
    clean_discord_message_url=PhotoSubmissionForm.clean_discord_message_url

    def __init__(self,*args,photo,**kwargs):
        super().__init__(*args,**kwargs)
        from .models import ChallengePrompt
        self.fields['prompt'].queryset=ChallengePrompt.objects.filter(category=photo.prompt_claim.category,submission_type='photo').order_by('number')
        self.fields['prompt'].label_from_instance=lambda p:f'{p.number}. {p.description}'


class PhotoCreditActionForm(forms.Form):
    version=forms.CharField(widget=forms.HiddenInput)
    reason=forms.CharField(required=False,max_length=1000,widget=forms.Textarea(attrs={'rows':3}))
