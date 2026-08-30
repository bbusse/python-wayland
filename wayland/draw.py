#!/usr/bin/env python3
import fcntl
import logging
import math
from PIL import Image
import sys
import os
import mmap
import cairocffi as cairo
import pangocffi
import pangocairocffi
import select
from io import BytesIO
import time
import wayland.protocol
from wayland.client import MakeDisplay, DisplayError, ServerDisconnected
from wayland.utils import AnonymousFile
import math


# See https://github.com/sde1000/python-xkbcommon for the following:
from xkbcommon import xkb

log = logging.getLogger(__name__)


# The pango foreground for a slot object; the caller sets font_colour_[rgb],
# and white is the fallback for a slot built without them
def font_colour(obj):
    return "#%02x%02x%02x" % (obj.get("font_colour_r", 255),
                              obj.get("font_colour_g", 255),
                              obj.get("font_colour_b", 255))

# The lists below live on the connection rather than at module level, so a
# process running one view per thread keeps them apart. Shared, one view's
# dead socket unregistered another's, one view's timer was alarmed by every
# other view's loop, and one view's shutdown ended all of them.
#
# eventlist:     future events; objects must support the nexttime attribute
#                and alarm() method. nexttime should be the time at which the
#                object next wants to be called, or None if the object
#                temporarily does not need to be scheduled.
# rdlist:        file descriptors to watch with handlers. Expected to be
#                objects with a fileno() method that returns the appropriate
#                fd number, and methods called doread(), dowrite(), etc.
# ticklist:      functions to invoke each time around the event loop. These
#                functions may do anything, including changing timeouts and
#                drawing on the display.
# preselectlist: functions to invoke before calling select. These functions
#                may not change timeouts or draw on the display. They will
#                typically flush queued output.

class time_guard(object):
    def __init__(self, name, max_time):
        self._name = name
        self._max_time = max_time
    def __enter__(self):
        self._start_time = time.time()
    def __exit__(self, type, value, traceback):
        t = time.time()
        time_taken = t - self._start_time
        if time_taken > self._max_time:
            log.info("time_guard: %s took %f seconds",self._name,time_taken)

tick_time_guard = time_guard("tick",0.5)
preselect_time_guard = time_guard("preselect",0.1)
doread_time_guard = time_guard("doread",0.5)
dowrite_time_guard = time_guard("dowrite",0.5)
doexcept_time_guard = time_guard("doexcept",0.5)
alarm_time_guard = time_guard("alarm",0.5)

# The compositor has gone away for this connection, either because it
# dropped us or because we sent it something it did not accept
connection_lost = (BrokenPipeError, ConnectionResetError,
                   ServerDisconnected, DisplayError)


def ping_handler(thing, serial):
    """
    Respond to a 'ping' with a 'pong'.
    """
    thing.pong(serial)

class Window:

    def __init__(self, connection, window, s_objects,
                 class_="python-wayland", redraw=None, fullscreen=False):
        self.s_objects = s_objects
        self.title = window["title"]
        self.orig_width = window["res_x"]
        self.orig_height = window["res_y"]
        self.view_num = window.get("view_num")
        self.view_count = window.get("view_count", 0)
        self._w = connection
        if not self._w.shm_formats:
            raise RuntimeError("No suitable Shm formats available")
        self.is_fullscreen = fullscreen
        self.redraw_func = redraw
        self.surface = self._w.compositor.create_surface()
        self._w.surfaces[self.surface] = self
        self.xdg_surface = self._w.xdg_wm_base.get_xdg_surface(self.surface)
        self.xdg_toplevel = self.xdg_surface.get_toplevel()
        self.xdg_toplevel.set_title(window["title"])
        self.xdg_toplevel.set_parent(None)
        self.xdg_toplevel.set_app_id(class_)
        self.xdg_toplevel.set_min_size(window["res_y"], window["res_x"])
        self.xdg_toplevel.set_max_size(window["res_y"], window["res_x"])

        if fullscreen:
            self.xdg_toplevel.set_fullscreen(None)

        self.wait_for_configure = True
        self.xdg_surface.dispatcher['configure'] = \
            self._xdg_surface_configure_handler

        #self.xdg_toplevel.dispatcher['configure'] = lambda *x: None
        #self.xdg_toplevel.dispatcher['close'] = lambda *x: None

        self.buffer = None
        self.shm_data = None
        self.commits = 0
        self.frames = 0
        self.frame_pending = False
        self.commit()

    def close(self):
        if not self.surface.destroyed:
            # The xdg objects hold the role for this surface and have to go
            # first, the compositor rejects a surface destroyed before its role
            self.xdg_toplevel.destroy()
            self.xdg_surface.destroy()
            self.surface.destroy()
            if self.buffer is not None:
                self.buffer.destroy()
                self.buffer = None
                self.shm_data.close()
                del self.s, self.shm_data

    def resize(self, width, height):
        # Do not complete a resize until configure has been acknowledged.
        # Checked before dropping anything, so a resize we are not going to
        # finish leaves the buffer we are still showing alone
        if self.wait_for_configure:
            return

        # A configure arrives whenever the compositor touches the window, most
        # of them asking for the size we already have. Reallocating the buffer
        # and redrawing for those costs an shm file and a full repaint each
        # time, so the existing buffer is presented again instead
        if self.buffer is not None and (width, height) == (self.width, self.height):
            self.redraw()
            return

        # Drop previous buffer and shm data if necessary. Cleared as well as
        # destroyed: the proxy stays truthy once destroyed, so a second
        # resize would otherwise call destroy() on it again
        if self.buffer is not None:
            self.buffer.destroy()
            self.buffer = None
            self.shm_data.close()
            self.shm_data = None

        wl_shm_format, cairo_shm_format = self._w.shm_formats[0]

        stride = cairo.ImageSurface.format_stride_for_width(
            cairo_shm_format, width)
        size = stride * height

        with AnonymousFile(size) as fd:
            self.shm_data = mmap.mmap(
                fd, size, prot=mmap.PROT_READ | mmap.PROT_WRITE,
                flags=mmap.MAP_SHARED)
            pool = self._w.shm.create_pool(fd, size)
            self.buffer = pool.create_buffer(
                0, width, height, stride, wl_shm_format)
            pool.destroy()
        self.s = cairo.ImageSurface(cairo_shm_format, width, height,
                                    data=self.shm_data, stride=stride)
        self.surface.attach(self.buffer, 0, 0)
        self.width = width
        self.height = height

        if self.redraw_func:
            # This should invoke `redraw` which then invokes `commit`
            self.redraw_func(self)
        else:
            self.commit()

    def commit(self):
        """Present the surface, asking the compositor to tell us when it did.

        commits counts what we submitted, frames counts what was actually put
        on screen, so the two differ when the compositor drops or throttles
        """
        if self.surface.destroyed:
            return

        try:
            callback = self.surface.frame()
            callback.dispatcher['done'] = self._frame_done
            self.frame_pending = True
        except Exception as e:
            log.debug("frame callback unavailable: %s", e)

        self.commits += 1
        self.surface.commit()

    # wl_callback has no destroy request, so the proxy outlives the frame and
    # can deliver done more than once. The flag counts one frame per commit,
    # without relying on which proxy object the event arrives on
    def _frame_done(self, callback, time_ms):
        if not self.frame_pending:
            return

        self.frame_pending = False
        self.frames += 1

    def redraw(self):
        """Copy the whole window surface to the display"""
        self.add_damage()
        self.commit()

    def add_damage(self, x=0, y=0, width=None, height=None):
        if width is None:
            width = self.width
        if height is None:
            height = self.height
        self.surface.damage(x, y, width, height)

    def pointer_motion(self, seat, time, x, y):
        pass

    def _xdg_surface_configure_handler(
            self, the_xdg_surface, serial):
        the_xdg_surface.ack_configure(serial)

        self.wait_for_configure = False
        if not self.surface.destroyed:
            self.resize(self.orig_width, self.orig_height)

class Seat:
    def __init__(self, obj, connection, global_name):
        self.c_enum = connection.interfaces['wl_seat'].enums['capability']
        self.s = obj
        self._c = connection
        self.global_name = global_name
        self.name = None
        self.capabilities = 0
        self.pointer = None
        self.keyboard = None
        self.s.dispatcher['capabilities'] = self._capabilities
        self.s.dispatcher['name'] = self._name
        self.tabsym = xkb.keysym_from_name("Tab")

    def removed(self):
        if self.pointer:
            self.pointer.release()
            self.pointer = None
        if self.keyboard:
            self.keyboard.release()
            del self.keyboard_state
            self.keyboard = None
        # ...that's odd, there's no request in the protocol to destroy
        # the seat proxy!  I suppose we just have to leave it lying
        # around.

    def _name(self, seat, name):
        log.debug("Seat got name: %s", name)
        self.name = name

    def _capabilities(self, seat, c):
        log.debug("Seat %s got capabilities: %s", self.name, c)
        self.capabilities = c
        pointer_available = c & self.c_enum['pointer']
        if pointer_available and not self.pointer:
            self.pointer = self.s.get_pointer()
            self.pointer.dispatcher['enter'] = self.pointer_enter
            self.pointer.dispatcher['leave'] = self.pointer_leave
            self.pointer.dispatcher['motion'] = self.pointer_motion
            self.pointer.silence['motion'] = True
            self.pointer.dispatcher['button'] = self.pointer_button
            self.pointer.dispatcher['axis'] = self.pointer_axis
            self.current_pointer_window = None
        if self.pointer and not pointer_available:
            self.pointer.release()
            self.current_pointer_window = None
            self.pointer = None
        keyboard_available = c & self.c_enum['keyboard']
        if keyboard_available and not self.keyboard:
            self.keyboard = self.s.get_keyboard()
            self.keyboard.dispatcher['keymap'] = self.keyboard_keymap
            self.keyboard.dispatcher['enter'] = self.keyboard_enter
            self.keyboard.dispatcher['leave'] = self.keyboard_leave
            self.keyboard.dispatcher['key'] = self.keyboard_key
            self.keyboard.dispatcher['modifiers'] = self.keyboard_modifiers
            self.current_keyboard_window = None
        if self.keyboard and not keyboard_available:
            self.keyboard.release()
            self.current_keyboard_window = None
            self.keyboard_state = None
            self.keyboard = None

    def pointer_enter(self, pointer, serial, surface, surface_x, surface_y):
        log.debug("pointer_enter %s %s %s %s",
                  serial, surface, surface_x, surface_y)
        self.current_pointer_window = self._c.surfaces.get(surface, None)
        pointer.set_cursor(serial, None, 0, 0)

    def pointer_leave(self, pointer, serial, surface):
        log.debug("pointer_leave %s %s", serial, surface)
        self.current_pointer_window = None

    def pointer_motion(self, pointer, time, surface_x, surface_y):
        if not self.current_pointer_window:
            raise Exception("Pointer motion encountered even though there is not a matching window")
        self.current_pointer_window.pointer_motion(
            self, time, surface_x, surface_y)

    def pointer_button(self, pointer, serial, time, button, state):
        log.debug("pointer_button %s %s %s %s", serial, time, button, state)
        if state == 1 and self.current_pointer_window:
            log.debug("Seat %s starting shell surface move", self.name)
            self.current_pointer_window.xdg_toplevel.move(self.s, serial)

    def pointer_axis(self, pointer, time, axis, value):
        log.debug("pointer_axis %s %s %s", time, axis, value)

    def keyboard_keymap(self, keyboard, format_, fd, size):
        log.debug("keyboard_keymap %s %s %s", format_, fd, size)
        keymap_data = mmap.mmap(
            fd, size, prot=mmap.PROT_READ, flags=mmap.MAP_PRIVATE)
        os.close(fd)
        # The provided keymap appears to have a terminating NULL which
        # xkbcommon chokes on.  Specify length=size-1 to remove it.
        keymap = self._c.xkb_context.keymap_new_from_buffer(
            keymap_data, length=size - 1)
        keymap_data.close()
        self.keyboard_state = keymap.state_new()

    def keyboard_enter(self, keyboard, serial, surface, keys):
        log.debug("keyboard_enter %s %s %s", serial, surface, keys)
        self.current_keyboard_window = self._c.surfaces.get(surface, None)

    def keyboard_leave(self, keyboard, serial, surface):
        log.debug("keyboard_leave %s %s", serial, surface)
        self.current_keyboard_window = None

    def keyboard_key(self, keyboard, serial, time, key, state):
        log.debug("keyboard_key %s %s %s %s", serial, time, key, state)
        sym = self.keyboard_state.key_get_one_sym(key + 8)
        if state == 1 and sym == self.tabsym:
            # Why did I put this in?!
            log.debug("Saw a tab!")
        if state == 1:
            s = self.keyboard_state.key_get_string(key + 8)
            log.debug("s=%r", s)
            if s == "q":
                self._c.shutdowncode = 0
            elif s == "c":
                # Close the window
                self.current_keyboard_window.close()
            elif s == "f":
                # Fullscreen toggle
                if self.current_keyboard_window.is_fullscreen:
                    self.current_keyboard_window.xdg_toplevel.unset_fullscreen()
                    self.current_keyboard_window.is_fullscreen = False
                    self.current_keyboard_window.resize(
                        self.current_keyboard_window.orig_width,
                        self.current_keyboard_window.orig_height)
                else:
                    self.current_keyboard_window.xdg_toplevel.set_fullscreen(None)
                    self.current_keyboard_window.is_fullscreen = True

    def keyboard_modifiers(self, keyboard, serial, mods_depressed,
                           mods_latched, mods_locked, group):
        log.debug("keyboard_modifiers %s %s %s %s %s",
                  serial, mods_depressed, mods_latched, mods_locked, group)
        self.keyboard_state.update_mask(mods_depressed, mods_latched,
                                        mods_locked, group, 0, 0)

class Output:
    def __init__(self, obj, connection, global_name):
        self.o = obj
        self._c = connection
        self.global_name = global_name
        self.o.dispatcher['geometry'] = self._geometry
        self.o.dispatcher['mode'] = self._mode
        self.o.dispatcher['done'] = self._done

    def _geometry(self, output, x, y, phy_width, phy_height, subpixel,
                  make, model, transform):
        log.debug("Output: got geometry: x=%s, y=%s, phy_width=%s, "
                 "phy_height=%s, make=%s, model=%s",
                 x, y, phy_width, phy_height, make, model)

    def _mode(self, output, flags, width, height, refresh):
        log.debug("Output: got mode: flags=%s, width=%s, height=%s, "
                 "refresh=%s", flags, width, height, refresh)

    def _done(self, output):
        log.debug("Output: done for now")

class Waker:
    """A pipe in rdlist, so stop() can interrupt a blocking select."""

    def __init__(self):
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)

    def fileno(self):
        return self.r

    def doread(self):
        try:
            os.read(self.r, 64)
        except BlockingIOError:
            pass

    def dowrite(self):
        pass

    def doexcept(self):
        pass

    def wake(self):
        try:
            os.write(self.w, b"w")
        except OSError:
            pass

    def close(self):
        for fd in (self.r, self.w):
            try:
                os.close(fd)
            except OSError:
                pass


class WaylandConnection:
    def __init__(self, wp_base, *other_wps):
        self.shutdowncode = None
        self.eventlist = []
        self.rdlist = []
        self.ticklist = []
        self.preselectlist = []
        self.wps = (wp_base,) + other_wps
        self.interfaces = {}
        for wp in self.wps:
            for k,v in wp.interfaces.items():
                self.interfaces[k] = v

        # Create the Display proxy class from the protocol
        Display = MakeDisplay(wp_base)
        self.display = Display()

        self.registry = self.display.get_registry()
        self.registry.dispatcher['global'] = self.registry_global_handler
        self.registry.dispatcher['global_remove'] = \
            self.registry_global_remove_handler

        self.xkb_context = xkb.Context()

        # Dictionary mapping surface proxies to Window objects
        self.surfaces = {}

        self.compositor = None
        self.xdg_wm_base = None
        self.shm = None
        self.shm_formats = []
        self.seats = []
        self.outputs = []

        # Bind to the globals that we're interested in. NB we won't
        # pick up things like shm_formats at this point; after we bind
        # to wl_shm we need another roundtrip before we can be sure to
        # have received them.
        self.display.roundtrip()

        if not self.compositor:
            raise RuntimeError("Compositor not found")
        if not self.xdg_wm_base:
            raise RuntimeError("xdg_wm_base not found")
        if not self.shm:
            raise RuntimeError("Shm not found")

        # Pick up shm formats
        self.display.roundtrip()

        self.waker = Waker()
        self.rdlist.append(self)
        self.rdlist.append(self.waker)
        self.preselectlist.append(self._preselect)

    def fileno(self):
        return self.display.get_fd()

    def stop(self, code=0):
        self.shutdowncode = code
        self.waker.wake()

    def disconnect(self):
        self.waker.close()
        self.display.disconnect()

    def doread(self):
        self.display.recv()
        self.display.dispatch_pending()

    def eventloop(self):
        while self.shutdowncode is None:
            for i in self.ticklist:
                with tick_time_guard:
                    i()

            timeout = None
            t = time.time()
            for i in self.eventlist:
                nt = i.nexttime
                i.mainloopnexttime = nt
                if nt is None:
                    continue
                if timeout is None or (nt - t) < timeout:
                    timeout = max(nt - t, 0)

            for i in list(self.preselectlist):
                with preselect_time_guard:
                    try:
                        i()
                    except connection_lost as e:
                        log.warning("eventloop: preselect failed, dropping: %s", e)
                        if i in self.preselectlist:
                            self.preselectlist.remove(i)

            if self not in self.rdlist:
                log.warning("eventloop: connection is gone, stopping")
                self.shutdowncode = 1
                break

            try:
                (rd, wr, ex) = select.select(self.rdlist, [], [], timeout)
            except KeyboardInterrupt:
                (rd, wr, ex) = [], [], []
                self.shutdowncode = 1

            for i in rd:
                with doread_time_guard:
                    try:
                        i.doread()
                    except connection_lost as e:
                        log.warning("eventloop: read failed, dropping: %s", e)
                        if i in self.rdlist:
                            self.rdlist.remove(i)
            for i in wr:
                with dowrite_time_guard:
                    i.dowrite()
            for i in ex:
                with doexcept_time_guard:
                    i.doexcept()

            t = time.time()
            for i in self.eventlist:
                if not hasattr(i, 'mainloopnexttime'):
                    continue
                if i.mainloopnexttime and t >= i.mainloopnexttime:
                    with alarm_time_guard:
                        i.alarm()

    def _preselect(self):
        self.display.flush()

    def registry_global_handler(self, registry, name, interface, version):
        log.debug("registry_global_handler: %s is %s v%s",
                 name, interface, version)
        if interface == "wl_compositor":
            # We know up to and require version 3
            self.compositor = registry.bind(
                name, self.interfaces['wl_compositor'], 3)
        elif interface == "xdg_wm_base":
            # We know up to and require version 1
            self.xdg_wm_base = registry.bind(
                name, self.interfaces['xdg_wm_base'], 1)
            self.xdg_wm_base.dispatcher['ping'] = ping_handler
        elif interface == "wl_shm":
            # We know up to and require version 1
            self.shm = registry.bind(
                name, self.interfaces['wl_shm'], 1)
            self.shm.dispatcher['format'] = self.shm_format_handler
        elif interface == "wl_seat":
            # We know up to and require version 4
            self.seats.append(Seat(registry.bind(
                name, self.interfaces['wl_seat'], 4), self, name))
        elif interface == "wl_output":
            # We know up to and require version 2
            self.outputs.append(Output(registry.bind(
                name, self.interfaces['wl_output'], 2), self, name))

    def registry_global_remove_handler(self, registry, name):
        # Haven't been able to get weston to send this event!
        log.debug("registry_global_remove_handler: %s gone", name)
        for s in self.seats:
            if s.global_name == name:
                log.debug("...it was a seat!  Releasing seat resources.")
                s.removed()

    def shm_format_handler(self, shm, format_):
        f = shm.interface.enums['format']
        if format_ == f.entries['argb8888'].value:
            self.shm_formats.append((format_, cairo.FORMAT_ARGB32))
        elif format_ == f.entries['xrgb8888'].value:
            self.shm_formats.append((format_, cairo.FORMAT_RGB24))
        elif format_ == f.entries['rgb565'].value:
            self.shm_formats.append((format_, cairo.FORMAT_RGB16_565))


def img_scale_down(img, canvas_x, canvas_y):
    img.thumbnail((canvas_x, canvas_y), Image.LANCZOS)
    return img


def img_scale_up(img, canvas_x, canvas_y, width, height):
    if not width or not height:
        return img

    scale = min(canvas_x / width, canvas_y / height)
    if scale <= 1:
        return img

    return img.resize((max(1, round(width * scale)),
                       max(1, round(height * scale))), Image.LANCZOS)


def draw_images_with_text(w, ctx=None):
    import os
    if not ctx:
        ctx = cairo.Context(w.s)
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    y = 40
    for idx, obj in enumerate(w.s_objects):
        file_path = obj.get("file")
        if file_path:
            logging.debug(f"draw_images_with_text: s_object[{idx}]['file'] = {file_path}")
            exists = os.path.isfile(file_path)
            logging.debug(f"draw_images_with_text: file exists: {exists}")
            if exists:
                try:
                    size = os.path.getsize(file_path)
                    logging.debug(f"draw_images_with_text: file size: {size}")
                    import imghdr
                    imgtype = imghdr.what(file_path)
                    logging.debug(f"draw_images_with_text: file type: {imgtype}")
                except Exception as e:
                    logging.error(f"draw_images_with_text: file stat/type error: {e}")
        if file_path and os.path.isfile(file_path):
            try:
                img = Image.open(file_path)
                buffer = BytesIO()
                img.save(buffer, format="PNG")
                buffer.seek(0)
                png = cairo.ImageSurface.create_from_png(buffer)
                ctx.set_source_surface(png, 40, y)
                ctx.paint()
                y += png.get_height() + 10
            except Exception as e:
                logging.error(f"draw_images_with_text: {e}")
        elif obj.get("text"):
            ctx.save()
            ctx.translate(40, y)
            layout = pangocairocffi.create_layout(ctx)
            font = obj.get("font") or obj.get("font_face") or "sans"
            font_size = obj.get("font_size") or 20
            markup = f'<span foreground="{font_colour(obj)}" font="{font} {font_size}">{obj["text"]}</span>'
            layout.apply_markup(markup)
            pangocairocffi.show_layout(ctx, layout)
            ctx.restore()
            y += 40
    draw_progress(w, ctx)
    del ctx
    w.s.flush()
    w.redraw()

def draw_image(w, ctx=False, text=False):
    if not ctx:
        ctx = cairo.Context(w.s)

        ctx.set_source_rgba(float(w.s_objects[0]["bg_colour_r"]/255),
                            float(w.s_objects[0]["bg_colour_g"]/255),
                            float(w.s_objects[0]["bg_colour_b"]/255),
                            w.s_objects[0]["bg_alpha"])
        ctx.set_operator(cairo.OPERATOR_SOURCE)
        ctx.paint()
        ctx.set_operator(cairo.OPERATOR_OVER)

    # SVG
    if w.s_objects[0]["file"].endswith(".svg") or \
       w.s_objects[0]["file"].endswith(".SVG"):
        return
    # Other image types
    else:
        img = Image.open(w.s_objects[0]["file"])
        width, height = img.size
        # Scale down if image exceeds screen size
        if w.s_objects[0]["img_scale_down"] and (width > w.orig_width or height > w.orig_height):
            img = img_scale_down(img, w.orig_width, w.orig_height)
            width, height = img.size
        # Scale up if image is smaller than screen
        elif w.s_objects[0]["img_scale_up"]:
            if height < w.orig_height or width < w.orig_width:
                img = img_scale_up(img, w.orig_width, w.orig_height, width, height)
                width, height = img.size

        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        png = cairo.ImageSurface.create_from_png(buffer)

    # Center image
    if w.s_objects[0]["alignment"] == "center":
        margin_left = (w.orig_width - width) / 2
        margin_top = (w.orig_height - height) / 2
    else:
        #margin_left = (w.orig_width - width)
        margin_left = 0
        margin_top = 0

    ctx.set_source_surface(png,
                           margin_left,
                           margin_top + w.s_objects[0]["offset_y"])

    ctx.paint()

    if text:
        return ctx

    draw_caption(w, ctx)
    draw_progress(w, ctx)

    w.s.flush()
    w.redraw()


def draw_caption(w, ctx, margin=40, pad=16):
    caption = ""
    for obj in w.s_objects[1:]:
        if obj.get("text"):
            caption = obj["text"]
            break

    if not caption:
        return

    obj = w.s_objects[1]
    ctx.identity_matrix()
    layout = pangocairocffi.create_layout(ctx)
    layout._set_width(pangocffi.units_from_double(w.orig_width - 2 * margin))
    layout._set_alignment(pangocffi.Alignment.CENTER)
    font = obj.get("font") or obj.get("font_face") or "sans"
    layout.apply_markup('<span foreground="white" font="{} {}">{}</span>'
                        .format(font, obj.get("font_size") or 20, caption))

    _, extents = layout.get_extents()
    height = pangocffi.units_to_double(extents.height)
    top = w.orig_height - margin - height - pad

    ctx.set_source_rgba(0, 0, 0, 0.55)
    ctx.rectangle(0, top - pad, w.orig_width, w.orig_height - top + pad)
    ctx.fill()

    ctx.move_to(margin, top)
    pangocairocffi.show_layout(ctx, layout)


def draw_image_with_context(w, ctx):
    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)


    del ctx

    w.s.flush()
    w.redraw()


def draw_text(w, ctx=None):
    if not ctx:
        logging.debug("draw-text: Creating canvas context for {} objects"\
                     .format(len(w.s_objects)))
        ctx = cairo.Context(w.s)

        ctx.set_source_rgba(float(w.s_objects[0]["bg_colour_r"]/255),
                            float(w.s_objects[0]["bg_colour_g"]/255),
                            float(w.s_objects[0]["bg_colour_b"]/255),
                            w.s_objects[0]["bg_alpha"])

    ctx.set_operator(cairo.OPERATOR_SOURCE)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)

    margin = 40
    layout = pangocairocffi.create_layout(ctx)
    layout._set_width(pangocffi.units_from_double(w.orig_width - 2 * margin))

    if w.s_objects[0]["alignment"] == "left":
        layout._set_alignment(pangocffi.Alignment.LEFT)
    elif w.s_objects[0]["alignment"] == "center":
        layout._set_alignment(pangocffi.Alignment.CENTER)

    markup = ""

    for obj in w.s_objects:
        logging.debug("draw-text: Adding pango markup block")

        fg = font_colour(obj)
        if not obj["font"]:
            # Use font-face
            markup += '<span foreground="{}" font="{} {}">{}\n</span>'\
                      .format(fg,
                      obj["font_face"],
                      obj["font_size"],
                      obj["text"])
        else:
            # Use font
            markup += '<span foreground="{}" font="{} {}">{}\n</span>'\
                      .format(fg,
                      obj["font"],
                      obj["font_size"],
                      obj["text"])

    logging.debug("draw-text: {}".format(markup))
    layout.apply_markup(markup)

    # A left-aligned block keeps its lines flush so a table's columns stay
    # aligned, but the block as a whole sits centered on the output. Centered
    # text already places its lines within the full layout width
    x = margin
    if w.s_objects[0]["alignment"] == "left":
        _, extents = layout.get_extents()
        text_width = pangocffi.units_to_double(extents.width)
        x = max(margin, (w.orig_width - text_width) / 2)

    ctx.translate(x, w.orig_height / 2 - 300)
    pangocairocffi.show_layout(ctx, layout)

    ctx.identity_matrix()
    draw_overlays(w, ctx)
    draw_progress(w, ctx)

    del ctx

    w.s.flush()
    w.redraw()


def draw_progress(w, ctx, radius=7, spacing=28, margin=24):
    count = getattr(w, "view_count", 0)
    num = getattr(w, "view_num", None)
    if not count or num is None:
        return

    ctx.identity_matrix()
    ctx.set_line_width(2)
    ctx.set_source_rgba(1, 1, 1, 0.8)
    x = (w.orig_width - (count - 1) * spacing) / 2
    y = w.orig_height - margin
    for n in range(count):
        ctx.new_path()
        ctx.arc(x + n * spacing, y, radius, 0, 2 * math.pi)
        if n == num:
            ctx.fill()
        else:
            ctx.stroke()


def draw_overlays(w, ctx, margin=40):
    x = w.orig_width - margin
    y = w.orig_height - margin
    for obj in w.s_objects[1:]:
        path = obj.get("file")
        if not path or not os.path.isfile(path):
            continue
        try:
            img = Image.open(path)
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            buffer.seek(0)
            png = cairo.ImageSurface.create_from_png(buffer)
        except Exception as e:
            logging.error("draw-text: overlay {}: {}".format(path, e))
            continue

        x -= png.get_width()
        ctx.set_source_surface(png, x, y - png.get_height())
        ctx.paint()
        x -= margin
