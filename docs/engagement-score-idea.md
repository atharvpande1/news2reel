 Instead of Shareability, we need to switch it for "Engagement Score". It should consitute of the following:

1. Geographic Specificity Tier: Make the LLM look at the title as a whole and return a single, overall "Geographic Specificity Tier" for the entire
article. Basically, if an article specifically names a well-known local park, town square, or major cross-streets, a higher Geographic Specificity Tier should be given to it.
Hyperlocal_Point (Highest Proximity): The text mentions a specific street, landmark, building, shop, school, or intersection.Examples: "Fire at Kamala Mills", "Water logging on MG Road", "Protest outside City High
School".
Neighborhood_Zone (High Proximity): The text does not name a specific street or venue, but it mentions a distinct suburb, area, or municipal
ward.Examples: "Power outage hits Bandra West", "New park coming up in Kothrud", "Traffic changes in Sitabuldi".
City_General (Medium Proximity): The text only mentions the city name as a whole or a city-wide government body without pinning it to a specific area.Examples: "Mumbai Police issues new traffic rules", "Nagpur Municipal Corporation announces budget".
Regional_External (Zero Proximity): The text covers state-wide, national, or international news that happens to impact the city but didn't originate there.Examples: "Maharashtra government changes school timings", "New central tax rules affect local businesses".

2. Community Impact Category
Three tiers:
Tier 1 (Highest Impact): Public safety alerts, weather/infrastructure disruptions (road closures, power outages), city taxes/zoning changes, school district updates.
Tier 2 (Medium Impact): New business openings, local restaurant reviews, high school sports championships, major community festivals.
Tier 3 (Lowest Impact): Regular routine meeting announcements, lost pet notices (unless highly viral), generic lifestyle fluff pieces.

Using these two categories, calculate a weighed score (in the backend code, not LLM). 