from homeshield.zones import point_in_polygon, scale_polygon

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
# U shape: the notch (4..6, 5..10) is outside.
U_SHAPE = [[0, 0], [10, 0], [10, 10], [6, 10], [6, 5], [4, 5], [4, 10], [0, 10]]


def test_point_in_square():
    assert point_in_polygon(5, 5, SQUARE)
    assert not point_in_polygon(15, 5, SQUARE)
    assert not point_in_polygon(-1, -1, SQUARE)


def test_concave_polygon_notch_is_outside():
    assert point_in_polygon(2, 8, U_SHAPE)
    assert point_in_polygon(8, 8, U_SHAPE)
    assert not point_in_polygon(5, 8, U_SHAPE)


def test_degenerate_polygon():
    assert not point_in_polygon(0, 0, [[0, 0], [1, 1]])


def test_scale_polygon_from_reference_frame():
    assert scale_polygon([[320, 240]], 640, 480, 1280, 720) == [[640.0, 360.0]]


from homeshield.db import init_db
from homeshield.zones import ZoneStore, box_share_inside, mark_fireplace_fires


def test_how_much_of_a_box_is_inside():
    assert box_share_inside((2, 2, 8, 8), SQUARE) == 1.0
    assert box_share_inside((20, 20, 30, 30), SQUARE) == 0.0
    assert abs(box_share_inside((5, 0, 15, 10), SQUARE) - 0.5) < 0.01


def test_fireplace_fires_are_marked_ignored():
    fires = [{"bbox": (1, 1, 9, 9)}, {"bbox": (5, 0, 25, 10)}, {"bbox": (40, 40, 50, 50)}]
    mark_fireplace_fires(fires, [{"polygon_scaled": SQUARE}])
    assert [d["ignored"] for d in fires] == [True, False, False]   # inside / spreading out / elsewhere


def test_zone_types_are_kept(tmp_path):
    db = str(tmp_path / "z.db")
    init_db(db)
    store = ZoneStore(db)
    store.add(zone_name="Hearth", camera_id=1, polygon=SQUARE, zone_type="fireplace")
    store.add(zone_name="Odd", camera_id=1, polygon=SQUARE, zone_type="nonsense")
    types = {z["zone_name"]: z["zone_type"] for z in ZoneStore(db).list_all()}
    assert types == {"Hearth": "fireplace", "Odd": "danger"}
    assert [z["zone_name"] for z in store.fireplace_zones_for(1, 640, 480)] == ["Hearth"]
