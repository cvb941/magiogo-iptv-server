import re
import roman


def parse_season_number(show_title):
    """Tries to parse season number in roman numbers from show title using regex. Bones IX. -> (Bones, 9)"""

    # Trim spaces
    show_title = show_title.strip()

    regex = r" M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\.?$"
    matches = re.search(regex, show_title)

    if matches:
        roman_num = matches.group()
        show_title_sans_season = show_title.replace(roman_num, "")
        # Remove leading space and trailing dot if present
        roman_num = roman_num.strip(' .')
        # Convert roman numeral to arabic
        return show_title_sans_season, roman.fromRoman(roman_num)
    else:
        return show_title, None
