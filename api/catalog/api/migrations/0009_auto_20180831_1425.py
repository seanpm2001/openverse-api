# Generated by Django 2.0.5 on 2018-08-31 14:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('api', '0008_imagelist_slug'),
    ]

    operations = [
        migrations.AddField(
            model_name='imagelist',
            name='auth',
            field=models.CharField(default='fdsadfwetyhegaerg', help_text='A randomly generated string assigned upon list creation. Used to authenticate updates and deletions.', max_length=64),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='imagelist',
            name='slug',
            field=models.CharField(help_text='A unique identifier used to make a friendly URL for downstream API consumers.', max_length=200, unique=True),
        ),
        migrations.AlterField(
            model_name='shortenedlink',
            name='full_url',
            field=models.URLField(db_index=True, max_length=1000, unique=True),
        ),
    ]