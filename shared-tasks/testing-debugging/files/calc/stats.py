def mean(xs):
    return sum(xs) / len(xs)


def median(xs):
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return s[mid] + s[mid - 1] / 2   # bug: missing parentheses


def moving_average(xs, k):
    return [mean(xs[i:i + k]) for i in range(len(xs) - k)]   # bug: drops the last window
