#!/usr/bin/env python3
"""
Human-like mouse movement and click simulation for Playwright.

Generates natural mouse trajectories using Bezier curves with slight
randomization, overshoot, and variable speed — mimicking real human behavior.

Usage:
    from browser.human_mouse import human_click, human_move_to

    # Click an element with natural mouse movement
    await human_click(page, '[data-testid="reply"]')

    # Or pass an ElementHandle directly
    element = page.query_selector('[data-testid="reply"]')
    human_click_element(page, element)

    # Move mouse to coordinates without clicking
    human_move_to(page, 500, 300)
"""

import math
import random
import time


def _bezier_point(t, p0, p1, p2, p3):
    """Calculate point on cubic Bezier curve at parameter t."""
    u = 1 - t
    return u**3 * p0 + 3 * u**2 * t * p1 + 3 * u * t**2 * p2 + t**3 * p3


def _generate_path(start_x, start_y, end_x, end_y, steps=None):
    """Generate a natural mouse path using cubic Bezier curves.

    Returns list of (x, y) waypoints that mimic human mouse movement:
    - Slight curve (not perfectly straight)
    - Random control points for natural arc
    - Variable step count based on distance
    """
    distance = math.sqrt((end_x - start_x) ** 2 + (end_y - start_y) ** 2)

    # More steps for longer distances, fewer for short ones
    if steps is None:
        steps = max(10, min(50, int(distance / 15)))

    # Generate control points with some randomness
    # Control point 1: slightly off the direct path
    spread = max(30, distance * 0.2)
    cp1_x = start_x + (end_x - start_x) * 0.25 + random.uniform(-spread, spread)
    cp1_y = start_y + (end_y - start_y) * 0.25 + random.uniform(-spread, spread)

    # Control point 2: slightly off the direct path, closer to end
    cp2_x = (
        start_x + (end_x - start_x) * 0.75 + random.uniform(-spread * 0.5, spread * 0.5)
    )
    cp2_y = (
        start_y + (end_y - start_y) * 0.75 + random.uniform(-spread * 0.5, spread * 0.5)
    )

    path = []
    for i in range(steps + 1):
        t = i / steps
        # Ease-in-out: accelerate at start, decelerate at end
        t = t * t * (3 - 2 * t)

        x = _bezier_point(t, start_x, cp1_x, cp2_x, end_x)
        y = _bezier_point(t, start_y, cp1_y, cp2_y, end_y)

        # Add tiny jitter (1-2 pixels) for realism
        if 0 < i < steps:
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)

        path.append((int(x), int(y)))

    return path


def _get_element_center(page, element):
    """Get the center coordinates of an element with slight randomization."""
    box = element.bounding_box()
    if not box:
        return None, None

    # Don't always click dead center — offset slightly within the element
    offset_x = random.uniform(-box["width"] * 0.15, box["width"] * 0.15)
    offset_y = random.uniform(-box["height"] * 0.15, box["height"] * 0.15)

    center_x = box["x"] + box["width"] / 2 + offset_x
    center_y = box["y"] + box["height"] / 2 + offset_y

    return int(center_x), int(center_y)


def _get_current_mouse_pos(page):
    """Get current mouse position (or random starting position if unknown)."""
    # Playwright doesn't track mouse position, so we use a reasonable default
    viewport = page.viewport_size
    if viewport:
        return (
            random.randint(int(viewport["width"] * 0.2), int(viewport["width"] * 0.8)),
            random.randint(
                int(viewport["height"] * 0.2), int(viewport["height"] * 0.8)
            ),
        )
    return (random.randint(200, 800), random.randint(150, 500))


def human_move_to(page, target_x, target_y, start_x=None, start_y=None):
    """Move mouse to coordinates with natural Bezier curve trajectory.

    Args:
        page: Playwright Page object
        target_x, target_y: Destination coordinates
        start_x, start_y: Starting coordinates (auto-detected if None)
    """
    if start_x is None or start_y is None:
        start_x, start_y = _get_current_mouse_pos(page)

    path = _generate_path(start_x, start_y, target_x, target_y)

    for x, y in path:
        page.mouse.move(x, y)
        # Variable speed: faster in middle, slower at start/end
        time.sleep(random.uniform(0.003, 0.015))


def human_click_element(page, element, double=False):
    """Click an element with natural mouse movement to it first.

    Args:
        page: Playwright Page object
        element: Playwright ElementHandle
        double: If True, double-click instead of single click

    Returns:
        True if click was performed, False if element not found/visible
    """
    center_x, center_y = _get_element_center(page, element)
    if center_x is None:
        return False

    # Move naturally to the element
    human_move_to(page, center_x, center_y)

    # Small pause before clicking (humans don't click instantly)
    time.sleep(random.uniform(0.05, 0.2))

    # Click at the position
    if double:
        page.mouse.dblclick(center_x, center_y)
    else:
        page.mouse.click(center_x, center_y)

    # Small pause after click
    time.sleep(random.uniform(0.05, 0.15))

    return True


def human_click(page, selector, timeout=5000, double=False):
    """Click element by CSS selector with natural mouse movement.

    Args:
        page: Playwright Page object
        selector: CSS selector string
        timeout: Max wait time for element in ms
        double: If True, double-click

    Returns:
        True if click was performed, False if element not found
    """
    try:
        element = page.wait_for_selector(selector, timeout=timeout, state="visible")
        if element:
            return human_click_element(page, element, double=double)
    except Exception:
        pass
    return False


def human_scroll(page, direction="down", distance=None):
    """Scroll with mouse wheel simulation instead of JS scrollBy.

    Args:
        page: Playwright Page object
        direction: "up" or "down"
        distance: Pixels to scroll (randomized if None)
    """
    if distance is None:
        distance = random.randint(200, 600)

    if direction == "up":
        distance = -distance

    # Move mouse to a random position in the viewport first
    viewport = page.viewport_size
    if viewport:
        x = random.randint(int(viewport["width"] * 0.3), int(viewport["width"] * 0.7))
        y = random.randint(int(viewport["height"] * 0.3), int(viewport["height"] * 0.7))
        page.mouse.move(x, y)
        time.sleep(random.uniform(0.05, 0.15))

    # Scroll in increments (like real scroll wheel)
    remaining = abs(distance)
    while remaining > 0:
        chunk = min(remaining, random.randint(50, 150))
        delta = chunk if direction == "down" else -chunk
        page.mouse.wheel(0, delta)
        remaining -= chunk
        time.sleep(random.uniform(0.02, 0.08))
