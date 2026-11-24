from django.db import migrations, models

CHOICES=[('restricted','Restricted — Own Team Only'),('unrestricted','Unrestricted — All Teams')]

class Migration(migrations.Migration):
    dependencies=[('core','0088_scheduled_report_cleanup')]
    operations=[
        migrations.AddField('challengemonth','host_review_access',models.CharField(max_length=12,null=True,blank=True,default=None,choices=CHOICES),preserve_default=False),
        migrations.AlterField('challengemonth','host_review_access',models.CharField(max_length=12,null=True,blank=True,default='restricted',choices=CHOICES)),
    ]
