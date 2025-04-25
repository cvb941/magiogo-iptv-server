import re
import roman


def parse_season_number(show_title):
    """Tries to parse season number in roman numbers from show title using regex. Bones IX. -> (Bones, 9)"""

    # Trim spaces
    show_title = show_title.strip()

    regex = r" M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})\.?$"
    matches = re.search(regex, show_title)

    if matches:
        roman_num = matches.group().strip(' .')
        if not roman_num:  # If the matched group is empty after stripping
            return show_title, None
            
        show_title_sans_season = show_title.replace(matches.group(), "")
        # Convert roman numeral to arabic
        try:
            return show_title_sans_season, roman.fromRoman(roman_num)
        except roman.InvalidRomanNumeralError:
            return show_title, None
    else:
        return show_title, None
