[out:json][timeout:120];
area(3600324269)->.a;
(
  way["highway"](area.a);
  way["barrier"](area.a);
  way["man_made"](area.a);
  way["aeroway"](area.a);
  relation["type"="multipolygon"]["barrier"](area.a);
);
out geom meta;
