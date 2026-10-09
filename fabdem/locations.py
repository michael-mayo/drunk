"""Reference sites: 57.344 km squares of real terrain centred on these points, half on cities and half on wild terrain.

Cities cover the settings CS2 cities are built in: coasts backed by mountains,
river valleys, mountain basins, lakes and plains. Wild sites are at least
~30 km from any large city: mountains, uplands, quiet coasts and plains.
All lie between 60 S and 60 N (FABDEM covers 60 S to 80 N, and the far north
is mostly ice and tundra). FABDEM has no data for Armenia and Azerbaijan (left
out of the Copernicus DEM it is built on), so Tbilisi is not used. Values are
(latitude, longitude) in degrees.
"""

CITIES = {
    # Coasts backed by hills or mountains.
    "Vancouver": (49.283, -123.121),
    "Seattle": (47.606, -122.332),
    "San Francisco": (37.775, -122.419),
    "Rio de Janeiro": (-22.906, -43.173),
    "Cape Town": (-33.925, 18.424),
    "Wellington": (-41.287, 174.776),
    "Genoa": (44.405, 8.946),
    "Hong Kong": (22.319, 114.169),
    "Valparaiso": (-33.047, -71.612),
    "Naples": (40.852, 14.268),
    "Marseille": (43.296, 5.370),
    "Busan": (35.180, 129.075),
    "Kobe": (34.690, 135.196),
    # Flat coasts.
    "Copenhagen": (55.676, 12.568),
    "Buenos Aires": (-34.604, -58.382),
    # River valleys and hills.
    "Pittsburgh": (40.441, -79.996),
    "Lyon": (45.764, 4.836),
    "Budapest": (47.498, 19.040),
    "Porto": (41.158, -8.629),
    "Lisbon": (38.722, -9.139),
    "Turin": (45.070, 7.687),
    # Mountain basins.
    "Salt Lake City": (40.761, -111.891),
    "Innsbruck": (47.269, 11.404),
    "Denver": (39.739, -104.990),
    "Kathmandu": (27.717, 85.324),
    "Medellin": (6.244, -75.581),
    "Mexico City": (19.433, -99.133),
    # Lakes and plains.
    "Geneva": (46.204, 6.143),
    "Munich": (48.135, 11.582),
    "Kansas City": (39.100, -94.579),
}

WILD = {
    # Mountains.
    "Bernese Alps": (46.450, 8.050),
    "Central Pyrenees": (42.650, 0.750),
    "Colorado Rockies": (39.100, -106.600),
    "Scottish Highlands": (57.100, -5.000),
    "Appalachians": (37.500, -81.500),
    "Southern Alps": (-43.500, 170.500),
    "Patagonian Andes": (-42.500, -71.900),
    "High Atlas": (31.200, -7.900),
    "Greater Caucasus": (43.000, 42.500),
    "Eastern Carpathians": (47.500, 25.000),
    "Japanese Alps": (36.300, 137.600),
    "Taiwan Central Range": (23.500, 121.000),
    "Sierra Nevada": (37.500, -119.000),
    # Uplands and hills.
    "Massif Central": (45.200, 2.900),
    "Black Forest": (48.000, 8.200),
    "Ozarks": (36.000, -93.000),
    "Mantiqueira": (-21.500, -45.500),
    "Tasmanian Highlands": (-42.000, 146.500),
    "Drakensberg": (-29.300, 29.400),
    "Ethiopian Highlands": (11.500, 38.500),
    # Coasts without large cities.
    "Ryfylke fjords": (59.300, 6.500),
    "Brittany coast": (48.600, -3.500),
    "Big Sur": (36.200, -121.600),
    "Dalmatian coast": (43.700, 15.900),
    "Maine coast": (44.300, -68.300),
    "Chilean fjords": (-45.500, -73.000),
    "Corsica": (42.200, 9.000),
    "Konkan coast": (14.500, 74.500),
    # Plains.
    "Nebraska plains": (41.500, -100.000),
    "Pampas": (-35.500, -61.500),
}
