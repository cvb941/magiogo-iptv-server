import unittest
from parse_season_number import parse_season_number


class SeasonParsing(unittest.TestCase):
    def test_trailing_space(self):

        title = "Alert COVID - Alert COVID "

        (show_title, number) = parse_season_number(title)

        self.assertEqual(show_title, "Alert COVID - Alert COVID")
        self.assertEqual(number, None)


if __name__ == '__main__':
    unittest.main()
