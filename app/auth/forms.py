"""Authentication forms."""

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, EqualTo, Length, Optional, Regexp, ValidationError

from app.utils.validators import (
    PLANNER_CATEGORIES,
    normalize_phone,
    validate_business_name,
    validate_email_address,
    validate_pacra_number,
    validate_pacra_reference,
    validate_password,
    validate_person_name,
)


def _person_name(_form, field):
    _value, error = validate_person_name(field.data)
    if error:
        raise ValidationError(error)


def _business_name(_form, field):
    _value, error = validate_business_name(field.data)
    if error:
        raise ValidationError(error)


def _email(_form, field):
    _value, error = validate_email_address(field.data)
    if error:
        raise ValidationError(error)


def _phone(_form, field):
    _value, error = normalize_phone(field.data)
    if error:
        raise ValidationError(error)


def _strong_password(_form, field):
    error = validate_password(field.data)
    if error:
        raise ValidationError(error)


class LoginForm(FlaskForm):
    email = StringField("Email address", validators=[DataRequired(), Length(max=120), Regexp(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", message="Enter a valid email address.")])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])
    submit = SubmitField("Log in")


class ClientRegisterForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), _person_name])
    email = StringField("Email address", validators=[DataRequired(), _email])
    phone = StringField("Phone number", validators=[DataRequired(), _phone])
    password = PasswordField("Password", validators=[DataRequired(), _strong_password])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", message="Passwords do not match.")])
    submit = SubmitField("Create account")


def _pacra_number(_form, field):
    _value, error = validate_pacra_number(field.data)
    if error:
        raise ValidationError(error)


def _pacra_reference(_form, field):
    _value, error = validate_pacra_reference(field.data)
    if error:
        raise ValidationError(error)


class PlannerRegisterForm(FlaskForm):
    business_name = StringField("Business or company name", validators=[DataRequired(), _business_name])
    pacra_number = StringField("PACRA registration number", validators=[DataRequired(), _pacra_number])
    pacra_reference = StringField("PACRA verification or reference code", validators=[Optional(), _pacra_reference])
    business_category = SelectField(
        "Business category",
        choices=[("", "Choose a category")] + [(item, item) for item in PLANNER_CATEGORIES],
        validators=[DataRequired(message="Choose a business category.")],
    )
    location = StringField("Business location (town or city)", validators=[DataRequired(), Length(min=2, max=120)])
    biography = TextAreaField("Business description", validators=[DataRequired(), Length(min=20, max=2000)])
    experience = TextAreaField("Experience and previous events", validators=[DataRequired(), Length(min=20, max=2000)])
    specialities = StringField("Specialities", validators=[DataRequired(), Length(min=2, max=300)])
    services = TextAreaField("Services offered", validators=[DataRequired(), Length(min=3, max=1000)])
    full_name = StringField("Contact person's full name", validators=[DataRequired(), _person_name])
    email = StringField("Email address", validators=[DataRequired(), _email])
    phone = StringField("Phone number", validators=[DataRequired(), _phone])
    business_address = StringField("Business address", validators=[DataRequired(), Length(min=5, max=255)])
    password = PasswordField("Password", validators=[DataRequired(), _strong_password])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", message="Passwords do not match.")])
    accept_terms = BooleanField("I accept the terms and conditions", validators=[DataRequired(message="Accept the terms and conditions to register.")])
    submit = SubmitField("Submit planner application")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired(), Length(max=128)])
    password = PasswordField("New password", validators=[DataRequired(), _strong_password])
    confirm_password = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", message="Passwords do not match.")])
    submit = SubmitField("Update password")
