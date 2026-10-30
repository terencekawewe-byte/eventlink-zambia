"""Planner and booking forms."""

from flask_wtf import FlaskForm
from wtforms import IntegerField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, NumberRange, Optional

from app.utils.validators import PLANNER_CATEGORIES


class PlannerProfileForm(FlaskForm):
    business_name = StringField("Business name", validators=[DataRequired(), Length(min=2, max=120)])
    business_category = SelectField(
        "Business category",
        choices=[("", "Choose a category")] + [(item, item) for item in PLANNER_CATEGORIES],
        validators=[Optional()],
    )
    business_address = StringField("Business address", validators=[Optional(), Length(min=5, max=255)])
    biography = TextAreaField("Biography", validators=[DataRequired(), Length(min=20, max=2000)])
    location = StringField("Location", validators=[DataRequired(), Length(min=2, max=120)])
    experience = TextAreaField("Experience", validators=[DataRequired(), Length(min=20, max=2000)])
    specialities = StringField("Specialities", validators=[DataRequired(), Length(min=2, max=300)])
    availability = TextAreaField("Availability", validators=[Optional(), Length(max=500)])
    submit = SubmitField("Save profile")


class PlannerServiceForm(FlaskForm):
    service_name = StringField("Service name", validators=[DataRequired(), Length(min=2, max=120)])
    description = TextAreaField("Description", validators=[DataRequired(), Length(min=5, max=1000)])
    pricing_type = SelectField(
        "Pricing",
        choices=[
            ("quotation", "Quotation"),
            ("starting_from", "Starting from"),
            ("fixed", "Fixed price"),
        ],
    )
    starting_price = StringField("Price in ZMW", validators=[Optional(), Length(max=12)])
    submit = SubmitField("Save service")


class QuotationForm(FlaskForm):
    amount = StringField("Quotation amount (ZMW)", validators=[DataRequired(), Length(max=12)])
    description = TextAreaField("What the quotation covers", validators=[DataRequired(), Length(min=5, max=2000)])
    submit = SubmitField("Send quotation")


class BookingRequestForm(FlaskForm):
    event_type = SelectField("Event type", validators=[DataRequired()])
    event_date = StringField("Event date", validators=[DataRequired()])
    start_time = StringField("Start time", validators=[DataRequired()])
    duration = StringField("Duration in hours", validators=[DataRequired(), Length(max=5)])
    venue = StringField("Venue or location", validators=[DataRequired(), Length(min=2, max=200)])
    guest_count = IntegerField("Expected guests", validators=[DataRequired(), NumberRange(min=1, max=100000)])
    budget = StringField("Budget in ZMW", validators=[DataRequired(), Length(max=12)])
    requirements = TextAreaField("Requirements", validators=[DataRequired(), Length(min=10, max=2000)])
    contact_method = SelectField(
        "Preferred contact method",
        choices=[("phone", "Phone"), ("email", "Email")],
        validators=[DataRequired()],
    )
    submit = SubmitField("Submit booking request")
