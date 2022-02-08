from django_evolution.mutations import AddField
from django.db import models


MUTATIONS = [
    AddField('ReviewRequestDraft', 'owner', models.ForeignKey,
             null=True, related_model='auth.User')
]
