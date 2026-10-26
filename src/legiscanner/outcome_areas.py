"""Fixed outcome-area taxonomy for impact scoring.

Default set is Fairfax County's 10 strategic plan community outcome areas.
Distinct from the free-form `topics` table: this is a stable classification
used for impact scoring across all tracked policy domains, not a
user-editable tag vocabulary. See db.seed_outcome_areas().
"""

FAIRFAX_OUTCOME_AREAS: list[dict] = [
    {
        "slug": "cultural-recreational",
        "name": "Cultural and Recreational Opportunities",
        "description": (
            "Access to quality arts, sports, recreation, and culturally enriching "
            "activities; inclusive, affordable, and diverse programming; addressing "
            "barriers like cost, transportation, and language; high-quality facilities."
        ),
    },
    {
        "slug": "economic-opportunity",
        "name": "Economic Opportunity",
        "description": (
            "A diverse and resilient economy, upward mobility, workforce preparation; "
            "removing barriers to employment (childcare, transportation), supporting "
            "small and minority-owned businesses, innovation and entrepreneurship."
        ),
    },
    {
        "slug": "effective-efficient-government",
        "name": "Effective and Efficient Government",
        "description": (
            "Responsible resource management, responsive and equitable government; "
            "customer satisfaction, inclusive community engagement, a representative "
            "workforce, sound financial management and transparency."
        ),
    },
    {
        "slug": "empowerment-vulnerable-residents",
        "name": "Empowerment and Support for Residents Facing Vulnerability",
        "description": (
            "Residents facing poverty, disability, or other barriers can access "
            "high-quality, coordinated services; proactive outreach, streamlined "
            "access to services, support toward self-sufficiency."
        ),
    },
    {
        "slug": "environment-energy",
        "name": "Environment and Energy",
        "description": (
            "A healthy, sustainable environment; air/water/land quality, sustainability "
            "and climate resilience; reducing greenhouse gas emissions, green "
            "infrastructure, renewable energy and conservation."
        ),
    },
    {
        "slug": "healthy-communities",
        "name": "Healthy Communities",
        "description": (
            "Equitable access to health services, physical and behavioral health, "
            "healthy behaviors; addressing health disparities, mental health and "
            "substance use services, healthy eating and active living."
        ),
    },
    {
        "slug": "housing-neighborhood-livability",
        "name": "Housing and Neighborhood Livability",
        "description": (
            "Safe, enjoyable, and affordable living experiences; supply and quality of "
            "affordable housing, homelessness prevention, mixed-income development, "
            "access to neighborhood amenities like parks, transit, and healthy food."
        ),
    },
    {
        "slug": "lifelong-education",
        "name": "Lifelong Education and Learning",
        "description": (
            "Inclusive, responsive, accessible learning at every life stage; early "
            "childhood education, academic achievement, career and technical "
            "education, digital literacy and adult learning."
        ),
    },
    {
        "slug": "mobility-transportation",
        "name": "Mobility and Transportation",
        "description": (
            "Efficient, affordable, safe movement of residents, businesses, and goods; "
            "expanding transit and active transportation, road and trail conditions, "
            "accessibility and safety for all users."
        ),
    },
    {
        "slug": "safety-security",
        "name": "Safety and Security",
        "description": (
            "Feeling safe at home, school, work, and in the community; timely and "
            "effective emergency response, fair administration of justice, community "
            "trust in public safety, critical infrastructure protection."
        ),
    },
]
