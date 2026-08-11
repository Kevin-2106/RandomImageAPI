from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database import (
    compute_image_hash,
    compute_folder_effective_weight,
    get_folder_weight_preview,
    get_random_image,
    set_folder_weight,
)
from models import Base, Image


def add_image(db: Session, path: str) -> None:
    p = Path(path)
    db.add(Image(path=path, folder=str(p.parent), filename=p.name, file_size=1))
    db.commit()


class WeightTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_nearest_rule_overrides_parent_instead_of_multiplying(self):
        weights = {"/images": 2.0, "/images/cats": 0.5}
        self.assertEqual(compute_folder_effective_weight("/images/cats/kitten", weights), 0.5)
        self.assertEqual(compute_folder_effective_weight("/images/dogs", weights), 2.0)
        self.assertEqual(compute_folder_effective_weight("/other", weights), 1.0)

    def test_preview_weights_folders_not_image_counts(self):
        add_image(self.db, "/library/a/1.jpg")
        add_image(self.db, "/library/a/2.jpg")
        add_image(self.db, "/library/b/3.jpg")
        set_folder_weight(self.db, "/library/a", 2)
        set_folder_weight(self.db, "/library/b", 1)

        preview = {row["folder_path"]: row for row in get_folder_weight_preview(self.db)}
        self.assertAlmostEqual(preview["/library/a"]["folder_probability"], 2 / 3)
        self.assertAlmostEqual(preview["/library/b"]["folder_probability"], 1 / 3)
        self.assertAlmostEqual(preview["/library/a"]["image_probability"], 1 / 3)

    def test_random_selection_respects_folder_weight(self):
        add_image(self.db, "/library/a/1.jpg")
        add_image(self.db, "/library/a/2.jpg")
        add_image(self.db, "/library/b/3.jpg")
        set_folder_weight(self.db, "/library/a", 2)
        set_folder_weight(self.db, "/library/b", 1)

        captured = {}

        def choose(population, weights, k):
            captured.update(population=population, weights=weights, k=k)
            return [population[0]]

        with patch("database.random.choices", choose):
            get_random_image(self.db)

        self.assertEqual(
            dict(zip(captured["population"], captured["weights"], strict=True)),
            {"/library/a": 2, "/library/b": 1},
        )

    def test_duplicate_files_share_real_sha256(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            first = Path(directory) / "first.jpg"
            second = Path(directory) / "second.jpg"
            first.write_bytes(b"same image bytes")
            second.write_bytes(b"same image bytes")
            add_image(self.db, str(first))
            add_image(self.db, str(second))
            images = self.db.query(Image).order_by(Image.id).all()

            self.assertEqual(
                compute_image_hash(self.db, images[0]),
                compute_image_hash(self.db, images[1]),
            )


if __name__ == "__main__":
    unittest.main()
