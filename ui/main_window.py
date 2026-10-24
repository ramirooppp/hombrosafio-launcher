"""Hombrosafío launcher presentation. Network/installation work stays in core/."""
import ctypes
import os
import queue
import re
import sys
import threading
import time
import math
from functools import lru_cache
from urllib.parse import urlparse

from PIL import Image, ImageTk, ImageDraw, ImageFilter, ImageFont
import customtkinter as ctk
import tkinter as tk

from core.config import load_config, save_config, get_base_hombrosafio_dir, resolve_server_ip, DEFAULT_MANIFEST_URL, DEFAULT_SERVER_IP
from core.server_pinger import ping_server
from core.sync import fetch_manifest, sync_mods
from core.runtime_manager import find_or_install_java_25, ensure_minecraft_and_fabric
from core.game_launcher import launch_minecraft
from core.launcher_updater import check_and_stage_update, start_self_update
from core.version import APP_VERSION

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

BG = "#0D1016"
PANEL = "#151922"
FIELD = "#1D2330"
LINE = "#2B3240"
TEXT = "#F3F1EA"
MUTED = "#9CA5B5"
GOLD = "#E9BC70"
GOLD_HOVER = "#F5D395"
GREEN = "#78D8AD"
RED = "#F08C8C"
FONT = "Segoe UI"
WHEEL_COLORS = ("#FFE64D","#FFAC30","#FF4051","#F443BB","#A765F5","#428CF5","#24E2E7","#6CEA48")


def get_asset_path(filename):
    base = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", filename)


def font(size, bold=False):
    return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")


def label(parent, text, size=13, color=TEXT, bold=False, **kwargs):
    kwargs.setdefault("height", size + 5)
    return ctk.CTkLabel(parent, text=text, font=font(size, bold), text_color=color, **kwargs)


@lru_cache(maxsize=1)
def windows_user32():
    """Keep HWND and message parameters pointer-sized on 64-bit Windows."""
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "GetParent": ((wintypes.HWND,), wintypes.HWND),
        "GetWindowLongW": ((wintypes.HWND, ctypes.c_int), wintypes.LONG),
        "SetWindowLongW": ((wintypes.HWND, ctypes.c_int, wintypes.LONG), wintypes.LONG),
        "SetWindowPos": ((wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, wintypes.UINT), wintypes.BOOL),
        "ReleaseCapture": ((), wintypes.BOOL),
        "PostMessageW": ((wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                          wintypes.LPARAM), wintypes.BOOL),
        "SendMessageW": ((wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                          wintypes.LPARAM), ctypes.c_ssize_t),
        "ShowWindow": ((wintypes.HWND, ctypes.c_int), wintypes.BOOL),
        "IsZoomed": ((wintypes.HWND,), wintypes.BOOL),
        "LoadImageW": ((wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                        ctypes.c_int, ctypes.c_int, wintypes.UINT), wintypes.HANDLE),
        "GetSystemMetrics": ((ctypes.c_int,), ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(user, name)
        function.argtypes = arguments
        function.restype = result
    return user


def dark_titlebar(window):
    if os.name == "nt":
        try:
            from ctypes import wintypes

            window_id = window.winfo_id()
            hwnd = windows_user32().GetParent(window_id) or window_id
            enabled = ctypes.c_int(1)
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            dwm.DwmSetWindowAttribute.argtypes = (
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            )
            dwm.DwmSetWindowAttribute.restype = wintypes.LONG
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled))
        except (AttributeError, OSError, tk.TclError):
            pass


class GlossButton(tk.Canvas):
    """Vector button with a bevel, soft reflection and mouse/keyboard states."""
    def __init__(self, master, **options):
        self.options = dict(options)
        self.gold = options.pop("gold", False)
        self.hero = options.pop("hero", False)
        self.hovered = self.pressed = False
        background = options.pop("background", PANEL)
        super().__init__(master, width=options.get("width", 140), height=options.get("height", 42),
                         bg=background, highlightthickness=0, bd=0, takefocus=True, cursor="hand2")
        self.wheel_source = None
        self.wheel_image = None
        self.wheel_size = None
        self.hero_surfaces = {}
        self.hero_surface = None
        self.play_word_image = None
        self.play_word_size = None
        if self.hero:
            path = get_asset_path("button_wheel.png")
            if os.path.isfile(path):
                with Image.open(path) as image:
                    self.wheel_source = image.copy()
        self.bind("<Configure>", lambda e: self._paint())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<space>", lambda e: self.invoke())
        self.bind("<Return>", self._keyboard)
        self.bind("<FocusIn>", lambda e: self._paint())
        self.bind("<FocusOut>", lambda e: self._paint())

    def configure(self, cnf=None, **kwargs):
        if cnf: kwargs.update(cnf)
        self.options.update(kwargs)
        self._paint()

    config = configure

    def cget(self, key):
        return self.options.get(key) if key in self.options else super().cget(key)

    def _keyboard(self, event):
        self.invoke()
        return "break"

    def _hover(self, active):
        self.hovered = active
        if not active: self.pressed = False
        self._paint()

    def _press(self, event):
        if self.options.get("state") != "disabled":
            self.focus_set(); self.pressed = True; self._paint()

    def _release(self, event):
        pressed = self.pressed
        self.pressed = False; self._paint()
        if pressed and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height(): self.invoke()

    def invoke(self):
        if self.options.get("state") != "disabled":
            callback = self.options.get("command")
            if callback: callback()

    @staticmethod
    def _mix(a, b, factor):
        first = tuple(int(a[i:i+2],16) for i in (1,3,5))
        second = tuple(int(b[i:i+2],16) for i in (1,3,5))
        return '#'+''.join(f'{round(x+(y-x)*factor):02x}' for x,y in zip(first,second))

    def _paint(self):
        self.delete("all")
        w,h=self.winfo_width(),self.winfo_height()
        if w < 3 or h < 3: return
        disabled = self.options.get("state") == "disabled"
        if self.hero:
            self._paint_hero(w, h, disabled)
            return
        top,bottom = ("#FFE3A0","#BD7E2D") if self.gold else ("#343E50","#1B2230")
        edge = "#F9D382" if self.gold else "#465269"
        if self.hovered: top=self._mix(top,"#FFFFFF",.15); bottom=self._mix(bottom,"#FFFFFF",.10)
        if self.pressed: top,bottom=bottom,top
        if disabled: top,bottom,edge="#343943","#242A34",LINE
        r=min(self.options.get("corner_radius",9),h/2-3)
        offset=2 if self.pressed else 0
        self.create_rectangle(r,h-4,w-r,h-1,fill="#07090D",outline="")
        for y in range(2,int(h-4)):
            dy=max(r-(y-2),(y-(h-5-r)),0)
            margin=r-math.sqrt(max(r*r-dy*dy,0))
            factor=(y-2)/max(h-7,1)
            color=self._mix(top,bottom,factor)
            self.create_line(2+margin,y+offset,w-3-margin,y+offset,fill=color)
        self.create_line(r+2,3+offset,w-r-3,3+offset,fill=edge)
        self.create_line(r+3,h-6,w-r-4,h-6,fill="#986021" if self.gold and not disabled else "#121824")
        if self.gold and not disabled:
            for y in range(5,int(h*.40)):
                self.create_line(r+4,y+offset,w-r-5,y+offset,fill=self._mix(top,bottom,(y-2)/max(h-7,1)*.7))
        if self.focus_get()==self:
            self.create_rectangle(5,5,w-6,h-7,outline=edge,dash=(2,3))
        text=self.options.get("text", "")
        text_font=self.options.get("font", (FONT,13))
        color="#17110A" if self.gold else TEXT
        if disabled: color=MUTED
        self.create_text(w/2,h/2-1+offset,text=text,font=text_font,fill=color)

    def _paint_hero(self, w, h, disabled):
        shift = 1.5 if self.pressed else 0
        mid = (h-4)/2 + shift
        key = (w, h, disabled, self.hovered, self.pressed)
        if key not in self.hero_surfaces:
            if len(self.hero_surfaces) >= 12:
                self.hero_surfaces.clear()
            self.hero_surfaces[key] = self._render_hero_surface(w,h,disabled)
        self.hero_surface = self.hero_surfaces[key]
        self.create_image(0,0,image=self.hero_surface,anchor="nw")
        cx,rad=29,min(25,(h-9)/2)
        if self.wheel_source is not None:
            diameter = max(1, round(rad*2+2))
            if self.wheel_size != diameter:
                self.wheel_image = ImageTk.PhotoImage(self.wheel_source.resize((diameter,diameter),Image.Resampling.LANCZOS),master=self)
                self.wheel_size = diameter
            self.create_image(cx,mid,image=self.wheel_image)
        text=self.options.get("text", "JUGAR")
        size=14 if len(text)>17 else 15
        tx=(76+w-20)/2
        if text == "JUGAR" and not disabled:
            if self.play_word_size != size:
                self.play_word_image = self._render_rainbow_word(size)
                self.play_word_size = size
            self.create_image(tx,mid,image=self.play_word_image)
        else:
            self.create_text(tx,mid+1,text=text,font=(FONT,size,"bold"),fill="#0C1017")
            self.create_text(tx,mid,text=text,font=(FONT,size,"bold"),fill=MUTED if disabled else "#FFF7E5")
        if self.focus_get()==self:
            self.create_line(86,h-18+shift,w-38,h-18+shift,fill=GOLD,dash=(2,3))

    def _render_rainbow_word(self, size):
        scale = 4
        font_path = os.path.join(os.environ.get("WINDIR",r"C:\Windows"),"Fonts","segoeuib.ttf")
        pixels = round(self.winfo_fpixels("1p")*size*scale)
        try:
            text_font = ImageFont.truetype(font_path,pixels)
        except OSError:
            text_font = ImageFont.load_default(size=pixels)
        bounds = text_font.getbbox("JUGAR")
        padding = 2*scale
        width,height=bounds[2]-bounds[0],bounds[3]-bounds[1]
        dimensions=(width+2*padding,height+2*padding)
        mask = Image.new("L",dimensions)
        ImageDraw.Draw(mask).text((padding-bounds[0],padding-bounds[1]),"JUGAR",font=text_font,fill=255)
        result = Image.new("RGBA",dimensions)
        result.paste("#080D15",(0,0),mask.filter(ImageFilter.MaxFilter(2*scale+1)))
        colors=WHEEL_COLORS
        fill = Image.new("RGBA",dimensions)
        draw=ImageDraw.Draw(fill)
        for x in range(dimensions[0]):
            position=max(0,min(1,(x-padding)/max(width-1,1)))*(len(colors)-1)
            index=min(int(position),len(colors)-2)
            color=self._mix(colors[index],colors[index+1],position-index)
            draw.line((x,0,x,dimensions[1]),fill=color)
        result.paste(fill,(0,0),mask)
        final_size=(round(dimensions[0]/scale),round(dimensions[1]/scale))
        return ImageTk.PhotoImage(result.resize(final_size,Image.Resampling.LANCZOS),master=self)

    def _render_hero_surface(self, w, h, disabled):
        """Render procedural UI geometry at 4×, then filter its angular edges."""
        scale = 4
        size = (w*scale,h*scale)
        surface = Image.new("RGBA",size,self["background"])
        shift = 1.5 if self.pressed else 0
        mid = (h-4)/2+shift
        def points(coords):
            return [(round(x*scale),round(y*scale)) for x,y in coords]
        def gradient(coords, stops):
            mask = Image.new("L",size)
            ImageDraw.Draw(mask).polygon(points(coords),fill=255)
            layer = Image.new("RGBA",size)
            draw = ImageDraw.Draw(layer)
            for y in range(size[1]):
                position = y/max(size[1]-1,1)
                for (start,a),(end,b) in zip(stops,stops[1:]):
                    if start <= position <= end:
                        color=self._mix(a,b,(position-start)/(end-start))
                        if disabled: color=self._mix(color,"#29313C",.75)
                        elif self.hovered: color=self._mix(color,"#FFF4D6",.09)
                        draw.line((0,y,size[0],y),fill=color)
                        break
            surface.paste(layer,(0,0),mask)
            return mask
        backing=[(2,4+shift),(w-16,4+shift),(w-3,mid),(w-16,h-7+shift),(2,h-7+shift)]
        shadow = Image.new("RGBA",size)
        ImageDraw.Draw(shadow).polygon(points([(x,y+3) for x,y in backing]),fill=(0,0,0,180))
        surface.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(1.5*scale)))
        gradient(backing,[(0,"#47515D"),(.18,"#71818D"),(.5,"#212B35"),(1,"#1C2530")])
        inset=[(4,6+shift),(w-17,6+shift),(w-6,mid),(w-17,h-9+shift),(4,h-9+shift)]
        gradient(inset,[(0,"#111A27"),(.45,"#101722"),(1,"#050A12")])
        rim=[(57,7+shift),(w-19,7+shift),(w-7,mid),(w-19,h-10+shift),(57,h-10+shift),(67,mid)]
        rim_mask=Image.new("L",size)
        ImageDraw.Draw(rim_mask).polygon(points(rim),fill=255)
        rainbow=Image.new("RGB",size)
        rainbow_draw=ImageDraw.Draw(rainbow)
        for x in range(size[0]):
            position=max(0,min(1,(x/scale-57)/max(w-64,1)))*(len(WHEEL_COLORS)-1)
            index=min(int(position),len(WHEEL_COLORS)-2)
            color=self._mix(WHEEL_COLORS[index],WHEEL_COLORS[index+1],position-index)
            if disabled: color=self._mix(color,"#47515E",.8)
            elif self.hovered: color=self._mix(color,"#FFFFFF",.12)
            rainbow_draw.line((x,0,x,size[1]),fill=color)
        shading=Image.new("RGB",size)
        shading_draw=ImageDraw.Draw(shading)
        metal_stops=[(0,"#858585"),(.12,"#FFFFFF"),(.25,"#C5C5C5"),(.44,"#777777"),(.61,"#B0B0B0"),(.79,"#F3F3F3"),(1,"#636363")]
        for y in range(size[1]):
            position=y/max(size[1]-1,1)
            for (start,a),(end,b) in zip(metal_stops,metal_stops[1:]):
                if start <= position <= end:
                    shading_draw.line((0,y,size[0],y),fill=self._mix(a,b,(position-start)/(end-start)))
                    break
        from PIL import ImageChops
        surface.paste(ImageChops.multiply(rainbow,shading),(0,0),rim_mask)
        # Broad diagonal specular reflection across the metal, clipped to its surface.
        reflection=Image.new("L",size)
        ImageDraw.Draw(reflection).polygon(points([(w*.45,-8),(w*.56,-8),(w*.37,h+8),(w*.26,h+8)]),fill=65)
        reflection=reflection.filter(ImageFilter.GaussianBlur(5*scale))
        reflection=ImageChops.multiply(reflection,rim_mask)
        surface.paste(Image.new("RGBA",size,"#F5FBFF"),(0,0),reflection)
        face=[(64,13+shift),(w-23,13+shift),(w-14,mid),(w-23,h-16+shift),(64,h-16+shift),(74,mid)]
        gradient(face,[(0,"#374352"),(.30,"#252E3A"),(.66,"#1C2430"),(1,"#121A25")])
        draw=ImageDraw.Draw(surface)
        draw.line(points([(58,8+shift),(w-19,8+shift),(w-8,mid)]),fill="#EAF6FF" if not disabled else "#6F747D",width=scale)
        draw.line(points([(67,14+shift),(w-24,14+shift),(w-15,mid)]),fill="#46505D",width=scale)
        return ImageTk.PhotoImage(surface.resize((w,h),Image.Resampling.LANCZOS),master=self)


class WindowChrome:
    """Custom header, retaining Windows resizing, taskbar and native window actions."""
    def _apply_taskbar_icon(self, hwnd=None):
        if os.name != "nt":
            return
        icon_path = get_asset_path("icon.ico")
        if not os.path.isfile(icon_path):
            return

        try:
            user = windows_user32()
            hwnd = hwnd or self._hwnd()
            if not getattr(self, "_native_icon_handles", None):
                small_size = user.GetSystemMetrics(49) or 16  # SM_CXSMICON
                large_size = user.GetSystemMetrics(11) or 32  # SM_CXICON
                sizes = {small_size, large_size}
                self._native_icon_handles = {}
                for size in sizes:
                    handle = user.LoadImageW(None, icon_path, 1, size, size, 0x0010)
                    if handle:
                        self._native_icon_handles[size] = handle

            small_size = user.GetSystemMetrics(49) or 16
            large_size = user.GetSystemMetrics(11) or 32
            small_icon = self._native_icon_handles.get(small_size)
            large_icon = self._native_icon_handles.get(large_size)
            if small_icon:
                user.SendMessageW(hwnd, 0x0080, 0, small_icon)  # WM_SETICON / ICON_SMALL
            if large_icon:
                user.SendMessageW(hwnd, 0x0080, 1, large_icon)  # WM_SETICON / ICON_BIG
        except (AttributeError, OSError, tk.TclError):
            pass

    def install_chrome(self):
        self._maximized = False
        self.chrome = ctk.CTkFrame(self, fg_color="#11151D", height=42, corner_radius=0)
        self.chrome.grid(row=0,column=0,sticky="ew")
        self.chrome.grid_columnconfigure(0,weight=1)
        self.chrome.bind("<ButtonPress-1>",self._drag_window)
        self.chrome.bind("<Double-Button-1>",lambda e:self._toggle_maximize())
        self.window_controls=[]
        for column,text,command in ((1,"−",self._minimize),(2,"□",self._toggle_maximize),(3,"×",self.destroy)):
            button=ctk.CTkButton(self.chrome,text=text,width=46,height=40,corner_radius=0,
                fg_color="transparent",hover_color="#8C313B" if column==3 else FIELD,
                text_color=TEXT,font=font(13 if column==1 else 17),command=command)
            button.grid(row=0,column=column,sticky="ns")
            self.window_controls.append(button)
        self.after(80,self._remove_native_caption)
        self.bind("<Map>",lambda e:self.after(20,self._remove_native_caption) if e.widget==self else None,add="+")
        self._resize_origin = None
        self.bind("<ButtonPress-1>",self._start_resize,add="+")
        self.bind("<B1-Motion>",self._resize_window,add="+")
        self.bind("<ButtonRelease-1>",lambda e:setattr(self,"_resize_origin",None),add="+")

    def _hwnd(self):
        window_id = self.winfo_id()
        return windows_user32().GetParent(window_id) or window_id

    def _remove_native_caption(self):
        if self._closing or os.name!="nt": return
        user=windows_user32()
        hwnd=self._hwnd()
        style=user.GetWindowLongW(hwnd,-16)
        # Remove the sizing frame as well: Windows 10 otherwise keeps a colored top strip.
        user.SetWindowLongW(hwnd,-16,style & ~0x00C40000)
        user.SetWindowPos(hwnd,0,0,0,0,0,0x0027)
        dark_titlebar(self)
        self._apply_taskbar_icon(hwnd)

    def _start_resize(self,event):
        if event.x_root >= self.winfo_rootx()+self.winfo_width()-14 and event.y_root >= self.winfo_rooty()+self.winfo_height()-14:
            self._resize_origin=(event.x_root,event.y_root,self.winfo_width(),self.winfo_height())

    def _resize_window(self,event):
        if self._resize_origin:
            x,y,w,h=self._resize_origin
            minimum=self.minsize()
            self.geometry(f"{max(minimum[0],w+event.x_root-x)}x{max(minimum[1],h+event.y_root-y)}")

    def _drag_window(self,event):
        if os.name=="nt" and not self._closing:
            user=windows_user32()
            hwnd=self._hwnd()
            # Queue the native move instead of entering its modal loop inside
            # a Tk callback through ctypes (which releases Python's GIL).
            position=(int(event.x_root) & 0xFFFF) | ((int(event.y_root) & 0xFFFF) << 16)
            user.ReleaseCapture()
            user.PostMessageW(hwnd,0x00A1,2,position)  # WM_NCLBUTTONDOWN / HTCAPTION
            return "break"

    def _minimize(self):
        if os.name=="nt": windows_user32().ShowWindow(self._hwnd(),6)
        else: self.iconify()

    def _toggle_maximize(self):
        if os.name=="nt":
            user=windows_user32()
            user.ShowWindow(self._hwnd(),9 if user.IsZoomed(self._hwnd()) else 3)
            self.window_controls[1].configure(text="❐" if user.IsZoomed(self._hwnd()) else "□")
        else: self.state("normal" if self.state()=="zoomed" else "zoomed")


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, config, on_save_callback):
        super().__init__(parent)
        self.config_data = config
        self.on_save_callback = on_save_callback
        self.title("Preferencias · Hombrosafío")
        self.geometry("610x570")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.transient(parent)
        self.after(80, self._activate)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=32, pady=28)
        label(body, "Preferencias", 27, bold=True).pack(anchor="w")
        label(body, "Conexión y comportamiento del launcher.", color=MUTED).pack(anchor="w", pady=(3, 23))
        label(body, "CONEXIÓN", 10, GOLD, True).pack(anchor="w", pady=(0, 12))
        label(body, "Dirección del servidor", 13, bold=True).pack(anchor="w", pady=(0, 6))
        self.entry_server = self._entry(body, config.get("server_ip", DEFAULT_SERVER_IP))
        label(body, "Dirección o dominio, con puerto si es necesario.", 11, MUTED).pack(anchor="w", pady=(5, 16))
        label(body, "Origen de las actualizaciones", 13, bold=True).pack(anchor="w", pady=(0, 6))
        self.entry_manifest = self._entry(body, config.get("manifest_url", ""))
        label(body, "URL del manifiesto del pack. Modificala solo si cambia su origen.", 11, MUTED).pack(anchor="w", pady=(5, 20))
        self.chk_close = ctk.CTkCheckBox(body, text="Cerrar el launcher al iniciar Minecraft", font=font(13),
            text_color=TEXT, fg_color=GOLD, hover_color=GOLD_HOVER, checkmark_color=BG,
            border_color=LINE, border_width=1, corner_radius=5, checkbox_width=20, checkbox_height=20)
        self.chk_close.pack(anchor="w")
        if config.get("close_on_launch", True):
            self.chk_close.select()
        self.error_label = label(body, "", 11, RED, anchor="w", wraplength=540, justify="left")
        self.error_label.pack(fill="x", pady=(8, 0))
        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(side="bottom", fill="x", pady=(12, 0))
        GlossButton(actions, gold=True, background=BG, text="Guardar cambios", width=160, height=42, corner_radius=9,
            font=font(13, True), fg_color=GOLD, hover_color=GOLD_HOVER, text_color=BG,
            command=self.save_and_close).pack(side="right", padx=(10, 0))
        GlossButton(actions, background=BG, text="Cancelar", width=110, height=42, corner_radius=9,
            font=font(13), fg_color=FIELD, hover_color=LINE, text_color=TEXT,
            command=self.destroy).pack(side="right")
        self.bind("<Escape>", lambda event: self.destroy())

    def _entry(self, parent, value):
        entry = ctk.CTkEntry(parent, height=42, corner_radius=9, font=font(13),
            fg_color=FIELD, border_color=LINE, border_width=1, text_color=TEXT)
        entry.insert(0, value)
        entry.pack(fill="x")
        return entry

    def _activate(self):
        if self.winfo_exists():
            dark_titlebar(self)
            self.grab_set()
            self.entry_server.focus_set()

    def save_and_close(self):
        server = self.entry_server.get().strip()
        manifest = self.entry_manifest.get().strip()
        if not server:
            self.error_label.configure(text="Ingresá la dirección del servidor.")
            self.entry_server.focus_set()
            return
        if not manifest:
            manifest = DEFAULT_MANIFEST_URL
        elif urlparse(manifest).scheme != "https" or not urlparse(manifest).netloc or urlparse(manifest).username or urlparse(manifest).password:
            self.error_label.configure(text="El origen debe ser una URL HTTPS sin credenciales.")
            self.entry_manifest.focus_set()
            return
        self.config_data.update(manifest_url=manifest, server_ip=server, close_on_launch=bool(self.chk_close.get()))
        save_config(self.config_data)
        if self.on_save_callback:
            self.on_save_callback()
        self.destroy()


class HombrosafioLauncher(WindowChrome, ctk.CTk):
    def __init__(self, *, preview_mode=False, config_override=None):
        super().__init__()
        self.preview_mode = preview_mode
        self.config = (dict(username="", ram_gb=6, close_on_launch=True,
                            manifest_url=DEFAULT_MANIFEST_URL, server_ip=DEFAULT_SERVER_IP)
                       if preview_mode else load_config())
        if config_override:
            self.config.update(config_override)
        self.is_busy = False
        self._closing = False
        self._stop_event = threading.Event()
        self._events = queue.Queue()
        self._settings = None
        self._native_icon_handles = {}
        self._pending_launcher_update = None
        self._launcher_update_started = False
        self._startup_splash = None
        self._startup_progress = None
        self._startup_progress_mode = None
        self._server_ping_started = False
        fallback_message = os.environ.pop("HOMBROSAFIO_UPDATE_FALLBACK", "")
        self._pending_fallback_message = fallback_message
        skip_update_check = os.environ.pop("HOMBROSAFIO_SKIP_LAUNCHER_UPDATE", "") == "1"
        self._startup_update_enabled = (
            not preview_mode and os.name == "nt"
            and getattr(sys, "frozen", False) and not skip_update_check
        )
        self.title("Hombrosafío · Actualizador" if self._startup_update_enabled else "Hombrosafío 4")
        self.geometry("560x350" if self._startup_update_enabled else "1120x760")
        self.minsize(560, 350) if self._startup_update_enabled else self.minsize(1000, 720)
        self.configure(fg_color=BG)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        icon = get_asset_path("icon.ico")
        if os.path.exists(icon):
            self.iconbitmap(icon)
            self.after(120, self._apply_taskbar_icon)
        self.load_images()
        if self._startup_update_enabled:
            self._show_startup_splash()
        else:
            self.build_ui()
        self.after(100, lambda: dark_titlebar(self))
        self.after(60, self._drain_events)
        if not preview_mode:
            if not self._startup_update_enabled and fallback_message and hasattr(self, "lbl_status"):
                self.lbl_status.configure(text=fallback_message, text_color=GOLD)
            if self._startup_update_enabled:
                self.after(150, self.start_launcher_update_check)
            else:
                self.start_server_ping_loop()

    def load_images(self):
        path = get_asset_path("logo_transparent.png")
        self.img_logo = None
        self.img_startup_logo = None
        if os.path.isfile(path):
            with Image.open(path) as source:
                image = source.copy()
            self.img_logo = ctk.CTkImage(light_image=image, dark_image=image, size=(480, 240))
            self.img_startup_logo = ctk.CTkImage(
                light_image=image, dark_image=image, size=(224, 112)
            )

    def build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.install_chrome()
        header = ctk.CTkFrame(self, fg_color=BG, corner_radius=0, height=80)
        header.grid(row=1, column=0, sticky="ew", padx=30, pady=(12, 0))
        header.grid_columnconfigure(0, weight=1)
        self.server_badge = ctk.CTkFrame(header, fg_color=PANEL, corner_radius=9, border_width=1, border_color=LINE)
        self.server_badge.grid(row=0, column=1, padx=(12, 14), pady=16)
        self.server_dot = label(self.server_badge, "●", 12, GOLD)
        self.server_dot.pack(side="left", padx=(12, 7), pady=8)
        self.lbl_server_status = label(self.server_badge, "Comprobando servidor", 12, MUTED)
        self.lbl_server_status.pack(side="left", padx=(0, 13), pady=8)
        self.btn_settings = GlossButton(header, background=BG, text="Preferencias", width=118, height=36, font=font(12),
            fg_color="transparent", border_width=1, border_color=LINE, hover_color=FIELD,
            corner_radius=9, text_color=TEXT, command=self.open_settings)
        self.btn_settings.grid(row=0, column=2)

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=2, column=0, sticky="nsew", padx=30, pady=(15, 0))
        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, minsize=354)
        content.grid_rowconfigure(0, weight=1)
        hero = ctk.CTkFrame(content, fg_color="#16191F", corner_radius=18, border_width=1, border_color=LINE)
        hero.grid(row=0, column=0, sticky="nsew", padx=(0, 22))
        hero.grid_columnconfigure(0, weight=1)
        hero.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(hero, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=30, pady=(26, 0))
        label(top, "T E M P O R A D A   0 4", 10, GOLD, True).pack(side="left")
        label(top, "LA COMUNIDAD · EL DESAFÍO", 9, MUTED).pack(side="right")

        self.art = tk.Canvas(hero, bg="#16191F", highlightthickness=0, bd=0, height=272)
        self.art.grid(row=1, column=0, sticky="nsew", padx=4, pady=(16, 0))
        self.art.bind("<Configure>", self._draw_art)
        self.logo_label = ctk.CTkLabel(hero, text="" if self.img_logo else "HOMBROSAFÍO 4",
            image=self.img_logo, font=font(42, True), text_color=GOLD, fg_color="#16191F")
        self.logo_label.place(relx=.5, rely=.48, anchor="center")

        intro = ctk.CTkFrame(hero, fg_color="transparent")
        intro.grid(row=2, column=0, sticky="ew", padx=32, pady=(4, 28))
        chips = ctk.CTkFrame(intro, fg_color="transparent")
        chips.pack(anchor="w")
        for text in ("MINECRAFT 26.3", "FABRIC", "HOMBROSAFÍO 4"):
            chip = ctk.CTkFrame(chips, fg_color="#20242D", corner_radius=6, border_width=1, border_color=LINE)
            chip.pack(side="left", padx=(0, 8))
            label(chip, text, 10, "#C1C6CF", True).pack(padx=10, pady=5)

        panel = ctk.CTkFrame(content, width=354, fg_color=PANEL, corner_radius=18, border_width=1, border_color=LINE)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(5, weight=1)
        title = ctk.CTkFrame(panel, fg_color="transparent")
        title.grid(row=0, column=0, sticky="ew", padx=26, pady=(20, 14))
        label(title, "Acceso al servidor", 24, TEXT, True, anchor="w").pack(anchor="w")
        label(title, "Configurá tu nombre y la memoria del juego.", 12, MUTED, anchor="w").pack(anchor="w", pady=(3, 0))
        profile = ctk.CTkFrame(panel, fg_color="transparent")
        profile.grid(row=1, column=0, sticky="ew", padx=26)
        label(profile, "Nombre de jugador", 12, TEXT, True, anchor="w").pack(anchor="w", pady=(0, 8))
        self.entry_username = ctk.CTkEntry(profile, placeholder_text="Tu nombre en Minecraft", height=44,
            corner_radius=9, fg_color=FIELD, border_color=LINE, border_width=1, font=font(14),
            text_color=TEXT, placeholder_text_color="#747F92")
        self.entry_username.pack(fill="x")
        if self.config.get("username"):
            self.entry_username.insert(0, self.config["username"])
        self.name_hint = label(profile, "Usá el mismo nombre en cada partida.", 11, MUTED, anchor="w")
        self.name_hint.pack(anchor="w", pady=(6, 0))
        self.entry_username.bind("<KeyRelease>", lambda event: self._clear_name_error())
        memory = ctk.CTkFrame(panel, fg_color="transparent")
        memory.grid(row=2, column=0, sticky="ew", padx=26, pady=(14, 0))
        label(memory, "Memoria asignada", 12, TEXT, True).pack(anchor="w", pady=(0, 8))
        self.seg_ram = ctk.CTkSegmentedButton(memory, values=["4 GB", "6 GB", "8 GB", "10 GB", "12 GB"],
            height=39, corner_radius=8, font=font(11, True), fg_color=FIELD,
            selected_color="#59472E", selected_hover_color="#6C5738", unselected_color=FIELD,
            unselected_hover_color=LINE, text_color=TEXT, dynamic_resizing=False)
        ram = f"{self.config.get('ram_gb', 6)} GB"
        self.seg_ram.set(ram if ram in ("4 GB", "6 GB", "8 GB", "10 GB", "12 GB") else "6 GB")
        self.seg_ram.pack(fill="x")
        label(memory, "Memoria que Minecraft puede utilizar.", 11, MUTED).pack(anchor="w", pady=(6, 0))

        connection = ctk.CTkFrame(panel, fg_color="#11151D", corner_radius=9)
        connection.grid(row=3, column=0, sticky="ew", padx=26, pady=(14, 0))
        connection.grid_columnconfigure(0, weight=1)
        label(connection, "SERVIDOR", 9, MUTED, True, anchor="w").grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        self.server_address = label(connection, self.config["server_ip"], 11, TEXT, anchor="w", wraplength=200, justify="left")
        self.server_address.grid(row=1, column=0, sticky="w", padx=12, pady=(1, 10))
        self.btn_copy = ctk.CTkButton(connection, text="Copiar", width=58, height=28, corner_radius=6,
            fg_color="transparent", hover_color=FIELD, text_color=GOLD, font=font(11), command=self.copy_server)
        self.btn_copy.grid(row=0, column=1, rowspan=2, padx=(0, 10))

        status = ctk.CTkFrame(panel, fg_color="transparent")
        status.grid(row=4, column=0, sticky="ew", padx=26, pady=(10, 0))
        self.lbl_status = label(status, "Listo para preparar la partida.", 11, MUTED, anchor="w", wraplength=300, justify="left", height=30)
        self.lbl_status.pack(fill="x", pady=(0, 6))
        self.prog_bar = ctk.CTkProgressBar(status, height=4, corner_radius=2, progress_color=GOLD, fg_color=LINE)
        self.prog_bar.set(0)
        self.prog_bar.pack(fill="x")
        self.lbl_progress = label(status, "", 10, MUTED, anchor="e", height=15)
        self.lbl_progress.pack(fill="x", pady=(3, 0))
        action = ctk.CTkFrame(panel, fg_color="transparent")
        action.grid(row=6, column=0, sticky="ew", padx=26, pady=(4, 18))
        self.btn_play = GlossButton(action, gold=True, hero=True, text="JUGAR", height=64, corner_radius=10,
            font=font(15, True), fg_color=GOLD, hover_color=GOLD_HOVER, text_color="#20180E",
            text_color_disabled="#8F887D", command=self.start_play_thread)
        self.btn_play.pack(fill="x")
        label(action, "El pack se verifica antes de iniciar.", 10, MUTED).pack(pady=(8, 0))

        footer = ctk.CTkFrame(self, fg_color="transparent", height=46)
        footer.grid(row=3, column=0, sticky="ew", padx=32, pady=(10, 12))
        label(footer, f"Hombrosafío 4  /  Launcher de la comunidad  ·  v{APP_VERSION}", 10, "#707B8D").pack(side="left")
        ctk.CTkButton(footer, text="Abrir carpeta del juego  ↗", font=font(11), fg_color="transparent",
            text_color=MUTED, hover_color=PANEL, height=30, width=175,
            command=self.open_game_folder).pack(side="right")

    def _draw_art(self, event):
        canvas = self.art
        canvas.delete("all")
        w, h = event.width, event.height
        x, y = w / 2, h * .49
        # Quiet vector ornament; the existing brand image is the only hero bitmap.
        for radius, color in ((136, "#28251F"), (156, "#232326"), (183, "#202228")):
            canvas.create_oval(x-radius, y-radius, x+radius, y+radius, outline=color, width=1)
        canvas.create_line(22, y, w-22, y, fill="#22252C", dash=(3, 9))
        canvas.create_line(x, 20, x, h-20, fill="#22252C", dash=(3, 9))
        for px, py in ((.12,.2),(.83,.16),(.91,.62),(.18,.79),(.69,.86)):
            cx, cy = w*px, h*py
            canvas.create_line(cx-4, cy, cx+4, cy, fill="#5E503A")
            canvas.create_line(cx, cy-4, cx, cy+4, fill="#5E503A")

    def _enter_to_play(self, event):
        if self._settings is None or not self._settings.winfo_exists():
            self.start_play_thread()

    def _clear_name_error(self):
        self.entry_username.configure(border_color=LINE)
        self.name_hint.configure(text="Usá el mismo nombre en cada partida.", text_color=MUTED)

    def open_settings(self):
        if self.is_busy:
            return
        if self._settings is not None and self._settings.winfo_exists():
            self._settings.focus_set()
            return
        self._settings = SettingsDialog(self, self.config, self.on_settings_saved)

    def on_settings_saved(self):
        self.config = load_config()
        self.server_address.configure(text=self.config.get("server_ip", ""))
        self.lbl_status.configure(text="Preferencias guardadas.", text_color=GREEN)
        self.lbl_server_status.configure(text="Comprobando servidor")
        self.server_dot.configure(text_color=GOLD)

    def copy_server(self):
        self.clipboard_clear()
        self.clipboard_append(self.config.get("server_ip", ""))
        self.btn_copy.configure(text="Copiado")
        self.after(1800, lambda: self.btn_copy.configure(text="Copiar") if not self._closing else None)

    def open_game_folder(self):
        path = get_base_hombrosafio_dir()
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":
            os.startfile(path)

    def set_status(self, text):
        clean = str(text).lstrip("📡🔄☕🧵🚀✅❌⚠️⏳📥📦 ")
        self._events.put(("status", clean))

    def set_progress(self, value):
        self._events.put(("progress", max(0.0, min(1.0, float(value)))))

    def _drain_events(self):
        if self._closing:
            return
        for _ in range(200):
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.lbl_status.configure(text=value, text_color=MUTED)
            elif kind == "progress":
                self.prog_bar.set(value)
                self.lbl_progress.configure(text=f"{value:.0%}" if self.is_busy else "")
            elif kind == "server":
                self._show_server(value)
            elif kind == "finished":
                self._finish_launch(value)
            elif kind == "launcher_update_progress":
                message, progress = value
                self._set_startup_progress(message, progress)
            elif kind == "launcher_update_ready":
                self._pending_launcher_update = value
                self._set_startup_progress("Actualización lista. Instalando…", 1.0)
                self.after(500, self._apply_pending_launcher_update)
            elif kind == "launcher_update_done":
                self._finish_startup("Ya tenés la última versión. Iniciando…", after_ms=350)
            elif kind == "launcher_update_error":
                print(f"[Launcher Update] {value}")
                self._finish_startup(
                    "No se pudo revisar la actualización. Iniciando igual…",
                    color=MUTED,
                    after_ms=900,
                )
        self.after(60, self._drain_events)

    def _show_startup_splash(self):
        self.resizable(False, False)
        self._startup_splash = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=18,
                            border_width=1, border_color=LINE)
        self._startup_splash.pack(fill="both", expand=True, padx=18, pady=18)
        card = self._startup_splash
        if self.img_startup_logo is not None:
            ctk.CTkLabel(card, text="", image=self.img_startup_logo).pack(pady=(9, 0))
        else:
            label(card, "HOMBROSAFÍO 4", 22, GOLD, True).pack(pady=(16, 0))
        label(card, f"Actualizador del launcher · v{APP_VERSION}", 12, MUTED).pack(pady=(0, 0))
        self._startup_status = label(card, "Buscando actualizaciones…", 13, TEXT)
        self._startup_status.pack(pady=(19, 10))
        self._startup_progress = ctk.CTkProgressBar(
            card, width=400, height=8, corner_radius=4,
            progress_color=GOLD, fg_color=LINE, mode="indeterminate",
        )
        self._startup_progress.pack()
        self._startup_progress.start()
        self._startup_progress_mode = "indeterminate"
        self._startup_percent = label(card, "", 11, MUTED)
        self._startup_percent.pack(pady=(8, 0))

    def _set_startup_progress(self, message, progress=None):
        splash = self._startup_splash
        if splash is None or not splash.winfo_exists():
            return
        self._startup_status.configure(text=message)
        if progress is None:
            if self._startup_progress_mode != "indeterminate":
                self._startup_progress.stop()
                self._startup_progress.configure(mode="indeterminate")
                self._startup_progress.start()
                self._startup_progress_mode = "indeterminate"
            self._startup_percent.configure(text="")
            return
        if self._startup_progress_mode != "determinate":
            self._startup_progress.stop()
            self._startup_progress.configure(mode="determinate")
            self._startup_progress_mode = "determinate"
        value = max(0.0, min(1.0, float(progress)))
        self._startup_progress.set(value)
        self._startup_percent.configure(text=f"{value:.0%}")

    def _finish_startup(self, message=None, color=MUTED, after_ms=0):
        def show_launcher():
            splash = self._startup_splash
            self._startup_splash = None
            if splash is not None and splash.winfo_exists():
                try:
                    self._startup_progress.stop()
                except (AttributeError, tk.TclError):
                    pass
                splash.destroy()
            if self._closing:
                return
            self.geometry("1120x760")
            self.minsize(1000, 720)
            self.resizable(True, True)
            self.title("Hombrosafío 4")
            self.build_ui()
            self.bind("<Return>", self._enter_to_play)
            self.after(100, lambda: dark_titlebar(self))
            if message:
                self.lbl_status.configure(text=message, text_color=color)
            elif getattr(self, "_pending_fallback_message", None):
                self.lbl_status.configure(text=self._pending_fallback_message, text_color=GOLD)
                self._pending_fallback_message = None
            self.start_server_ping_loop()

        if after_ms:
            self.after(after_ms, show_launcher)
        else:
            show_launcher()

    def start_launcher_update_check(self):
        if self._launcher_update_started or self._closing or self.preview_mode:
            return
        if os.name != "nt" or not getattr(sys, "frozen", False):
            return
        self._launcher_update_started = True

        def worker():
            try:
                staged_path = check_and_stage_update(
                    status_callback=lambda message, progress=None: self._events.put(
                        ("launcher_update_progress", (message, progress))
                    )
                )
            except Exception as error:
                self._events.put(("launcher_update_error", error))
            else:
                if staged_path:
                    self._events.put(("launcher_update_ready", staged_path))
                else:
                    self._events.put(("launcher_update_done", None))

        threading.Thread(target=worker, daemon=True, name="launcher-update").start()

    def _apply_pending_launcher_update(self):
        staged_path = self._pending_launcher_update
        if not staged_path or self._closing:
            return
        if self.is_busy:
            self.after(500, self._apply_pending_launcher_update)
            return
        try:
            start_self_update(staged_path, executable_path=sys.executable)
        except Exception as error:
            print(f"[Launcher Update] No se pudo iniciar la instalación: {error}")
            self._pending_launcher_update = None
            self._finish_startup(
                "No se pudo aplicar la actualización. Iniciando con esta versión…",
                color=RED,
                after_ms=900,
            )
            return
        self._pending_launcher_update = None
        self.destroy()

    def _show_server(self, result):
        if result.get("online"):
            self.lbl_server_status.configure(text=f"En línea  ·  {result.get('players_online', 0)}/{result.get('players_max', 0)} jugadores", text_color=GREEN)
            self.server_dot.configure(text_color=GREEN)
        else:
            self.lbl_server_status.configure(text="Servidor no disponible", text_color=MUTED)
            self.server_dot.configure(text_color=RED)

    def start_server_ping_loop(self):
        if self._server_ping_started:
            return
        self._server_ping_started = True
        def worker():
            while not self._stop_event.is_set():
                try:
                    result = ping_server(self.config.get("server_ip", ""), timeout=2.5)
                except (ValueError, OSError):
                    result = {"online": False}
                self._events.put(("server", result))
                self._stop_event.wait(12)
        threading.Thread(target=worker, daemon=True, name="server-status").start()

    def start_play_thread(self):
        if self.is_busy or self.preview_mode:
            return
        nick = self.entry_username.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", nick):
            self.entry_username.configure(border_color=RED)
            self.name_hint.configure(text="Usá 1–16 letras, números o guiones bajos.", text_color=RED)
            self.entry_username.focus_set()
            return
        ram_val = self.seg_ram.get()
        try:
            ram_gb = int(ram_val.split()[0]) if ram_val else self.config.get("ram_gb", 6)
        except Exception:
            ram_gb = self.config.get("ram_gb", 6) or 6
        self.config.update(username=nick, ram_gb=ram_gb)
        save_config(self.config)
        self.is_busy = True
        self.btn_play.configure(state="disabled", text="Preparando partida…")
        self.entry_username.configure(state="disabled")
        self.seg_ram.configure(state="disabled")
        self.btn_settings.configure(state="disabled")
        self.prog_bar.set(.05)
        self.lbl_status.configure(text="Preparando tu partida…", text_color=MUTED)
        threading.Thread(target=self.play_pipeline, args=(nick, self.config["ram_gb"], dict(self.config)),
            daemon=True, name="game-preparation").start()

    def play_pipeline(self, nick, ram_gb, launch_config=None):
        config = launch_config or dict(self.config)
        result = {"ok": False, "close": False}
        try:
            game_dir = get_base_hombrosafio_dir()
            mc_dir = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), ".minecraft")
            if not os.path.exists(mc_dir):
                mc_dir = game_dir
            self.set_status("Buscando actualizaciones del pack…")
            self.set_progress(.15)
            manifest, error = fetch_manifest(config.get("manifest_url"), game_dir)
            if manifest is None:
                self.set_status(f"{error}. Se usarán los mods locales.")
            else:
                self.set_status("Sincronizando los mods del servidor…")
                sync_mods(manifest, game_dir, status_callback=self.set_status,
                          progress_callback=lambda p: self.set_progress(.2 + p*.4))
            self.set_status("Comprobando Java 25…")
            self.set_progress(.65)
            java = find_or_install_java_25(game_dir, status_callback=self.set_status,
                progress_callback=lambda p: self.set_progress(.65 + p*.15))
            self.set_status("Preparando Minecraft y Fabric…")
            self.set_progress(.85)
            version = ensure_minecraft_and_fabric(mc_dir=mc_dir, game_version="26.3", loader_version="0.19.5", java_exe=java,
                status_callback=self.set_status, progress_callback=lambda p: self.set_progress(.85 + p*.1))
            self.set_status("Iniciando Minecraft…")
            self.set_progress(1)
            proc = launch_minecraft(version_id=version, mc_dir=mc_dir, game_dir=game_dir, username=nick,
                ram_gb=ram_gb, java_exe=java, server_ip=resolve_server_ip(config.get("server_ip"), manifest), status_callback=self.set_status)

            # Detectar si la JVM finaliza tempranamente con error
            time.sleep(1.2)
            exit_code = proc.poll() if hasattr(proc, "poll") else None
            if exit_code is not None and isinstance(exit_code, int) and exit_code != 0:
                log_file_path = getattr(proc, "log_file_path", None) or os.path.join(mc_dir, "launcher_game.log")
                if not os.path.isfile(log_file_path):
                    log_file_path = os.path.join(game_dir, "launcher_game.log")
                error_details = ""
                if os.path.isfile(log_file_path):
                    try:
                        with open(log_file_path, "r", encoding="utf-8", errors="ignore") as lf:
                            lines = [l.strip() for l in lf.readlines() if l.strip()]
                            if lines:
                                error_details = ": " + " | ".join(lines[-3:])
                    except Exception:
                        pass
                raise RuntimeError(f"El juego se cerró inmediatamente (código {exit_code}){error_details}")
            result = {"ok": True, "close": config.get("close_on_launch", True)}
        except Exception as error:
            print(f"[Error Launch] {error}")
            result["error"] = str(error)
        finally:
            self._events.put(("finished", result))

    def _finish_launch(self, result):
        self.is_busy = False
        self.entry_username.configure(state="normal")
        self.seg_ram.configure(state="normal")
        self.btn_settings.configure(state="normal")
        self.btn_play.configure(state="normal", text="JUGAR")
        self.lbl_progress.configure(text="")
        if result.get("ok"):
            self.lbl_status.configure(text="Minecraft iniciado. Nos vemos dentro.", text_color=GREEN)
            if result.get("close"):
                self.btn_play.configure(state="disabled", text="Minecraft iniciado")
                self.after(2000, self.destroy)
        else:
            self.lbl_status.configure(text=f"No se pudo iniciar: {result.get('error', 'error desconocido')}", text_color=RED)
            self.prog_bar.set(0)
        if self._pending_launcher_update:
            self.after(300, self._apply_pending_launcher_update)

    def destroy(self):
        if not self._closing:
            self._closing = True
            self._stop_event.set()
            super().destroy()
