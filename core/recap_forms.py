from django import forms
from .recaps import PERIODS, filter_choices


class RecapFilterForm(forms.Form):
    perspective = forms.ChoiceField(choices=[('challenge','Challenge Wide'),('team','Team Wide'),('reader','My Reading')])
    group = forms.ChoiceField(required=False, label='Group')
    challenge = forms.ChoiceField(required=False, label='Challenge')
    reader = forms.ChoiceField(required=False, label='Reader')
    team = forms.ChoiceField(required=False, label='Team')
    period = forms.ChoiceField(choices=list(PERIODS.items()))
    anchor = forms.DateField(required=False, label='Calendar reference date', widget=forms.DateInput(attrs={'type':'date'}),
        help_text='Any date in the desired month, quarter, or year. Blank means today in each Group’s timezone.')
    start = forms.DateField(required=False, label='From date', widget=forms.DateInput(attrs={'type':'date'}))
    end = forms.DateField(required=False, label='Through date', widget=forms.DateInput(attrs={'type':'date'}))

    def __init__(self, *args, viewer, **kwargs):
        super().__init__(*args, **kwargs)
        choices = filter_choices(viewer, perspective=self.data.get('perspective','reader'),
            group_id=self.data.get('group'), challenge_id=self.data.get('challenge'))
        self.fields['group'].choices = [('', 'All available Groups')] + choices['groups']
        self.fields['challenge'].choices = [('', 'All available Challenges')] + choices['challenges']
        personal = str(self.data.get('reader') or '')
        self_value = str(viewer.pk) if personal == str(viewer.pk) else ''
        self.fields['reader'].choices = [(self_value, 'My Reading')] + [
            (pk, f'Reader · {label}') for pk, label in choices['readers'] if pk != viewer.pk]
        self.fields['team'].choices = [('', 'All available teams · separate recaps')] + choices['teams']
        if self.data.get('perspective', 'reader') != 'reader' or not viewer.is_superuser:
            self.fields.pop('reader')
        if self.data.get('perspective', 'reader') != 'team':
            self.fields.pop('team')

        self.viewer = viewer

    def clean(self):
        data = super().clean()
        if self.data.get('perspective', 'reader') == 'reader' and not self.viewer.is_superuser:
            if str(self.data.get('reader') or '') not in ('', str(self.viewer.pk)):
                raise forms.ValidationError('Only Platform Administrators may select another Reader.')
        if data.get('period') == 'custom':
            if not data.get('start'): self.add_error('start','Choose a start date.')
            if not data.get('end'): self.add_error('end','Choose an end date.')
            if data.get('start') and data.get('end') and data['start'] > data['end']:
                self.add_error('end','Through date must be on or after From date.')
        return data
