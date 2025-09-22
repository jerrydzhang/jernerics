import re
import numpy as np
import inspect


def map_equation(func, library):
    name = func.__name__
    file = inspect.getfile(func)
    f = open(file)

    start = False
    term_list = []
    k = []
    for line in f:
        if start:
            if "return" in line.split():
                continue

            if "]" in line.split():
                break

            if line[-2] == ",":  # Remove trailing comma
                line = line[:-2]
            # Replace array access (x[n] -> xn)
            line = re.sub(r"x\[(\d+)\]", r"x\1", line)

            # Handle exponentiation (x ** y -> x^y)
            line = line.replace(" ** ", "^")

            # Replace k[n] with the actual value
            for i in range(len(k)):
                line = re.sub(r"k\[" + str(i) + r"\]", str(k[i]), line)

            # Split the line into terms while keeping track of signs
            terms = re.split(r"(?=[+-])", line)  # Keep + and - signs with the terms

            # Process each term
            parsed_terms = []
            for term in terms:
                if term.isspace():
                    continue

                # Split each term by multiplication (*) and strip spaces
                term = "".join(term.split())  # Remove all spaces

                components = re.split(r"\s*\*\s*", term)  # Split by multiplication (*)
                components = [component.strip() for component in term.split("*")]
                parsed_terms.append(components)

            for i, terms in enumerate(parsed_terms):
                if len(terms) == 1:
                    continue

                contains_constant = False
                try:
                    x1 = float(terms[0])
                    x2 = float(terms[1])
                    combined = x1 * x2
                    terms = terms[2:]
                    terms.insert(0, combined)

                    parsed_terms[i] = terms
                    contains_constant = True

                except ValueError:
                    pass

                if not contains_constant:
                    try:
                        x1 = float(terms[0])
                        terms[0] = x1
                        contains_constant = True

                    except ValueError:
                        pass

                # Combine non-constant terms into single str
                combined = terms[contains_constant]
                for term in terms[contains_constant + 1 :]:
                    combined = f"{combined} {term}"

                if contains_constant:
                    parsed_terms[i] = [terms[0], combined]
                else:
                    parsed_terms[i] = [combined]

            term_list.append(parsed_terms)

        if "def" in line.split() and "#" not in line.split() and name in line:
            # Use regex to find the value of k
            match = re.search(r"k=\[(.*?)\]", line)

            if match:
                k_value = match.group(1)  # Get the value inside the brackets
                # Convert the string representation of the list into an actual Python list
                k = eval(f"[{k_value}]")
            else:
                print("No constants provided.")
            start = True

    f.close()

    feature_names = library.get_feature_names()
    coef = np.zeros((len(term_list), len(feature_names)))
    for i, terms in enumerate(term_list):
        for term in terms:
            if len(term) == 1:
                if type(term[0]) == float:
                    coef[i, 0] = term[0]
                if term[0] in feature_names:
                    coef[i, feature_names.index(term[0])] = 1.0
            else:
                coef[i, feature_names.index(term[1])] = term[0]

    return coef
