"""Avisa por ntfy.sh cuando aparece un hueco de cita."""
import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = 'https://online.imedhospitales.com'
CITAS = f'{BASE}/Citas.aspx'
P = 'ctl00$MainContent$'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')
CENTROS = {'5': 'Burjassot', '54': 'Colón'}
ESPECIALIDAD = '18'
SUBCONSULTA = '59'
MAX_DIAS_CON_HORAS = 10
ESTADO = Path(__file__).with_name('estado.json')
# El calendario devuelve los días libres como DayCell_Click(this, event, 'dd/mm/aaaa 0:00:00'),
# con la comilla escapada como &#39; en la página y como \' dentro del callback.
RE_DIA = re.compile(r"DayCell_Click\(this, event, (?:&#39;|\\?')(\d{2}/\d{2}/\d{4})")


class ImedError(Exception):
    pass


def campos(html):
    soup = BeautifulSoup(html, 'html.parser')
    data = {}
    for inp in soup.select('form#form1 input[name]'):
        tipo = (inp.get('type') or 'text').lower()
        if tipo in ('submit', 'image', 'button'):
            continue
        if tipo in ('radio', 'checkbox') and not inp.has_attr('checked'):
            continue
        data[inp['name']] = inp.get('value', '')
    for sel in soup.select('form#form1 select[name]'):
        opt = sel.find('option', selected=True) or sel.find('option')
        data[sel['name']] = opt.get('value', '') if opt else ''
    return data


class Imed:
    def __init__(self, usuario, password):
        self.s = requests.Session()
        self.s.headers['User-Agent'] = UA
        r = self._get(f'{BASE}/Account/login?ReturnUrl=%2fCitas.aspx')
        # Login en dos pantallas: primero el nº de historia, luego la contraseña.
        for campo, valor in (('UserName', usuario), ('Password', password)):
            data = campos(r.text)
            data[f'{P}Login1${campo}'] = valor
            data[f'{P}Login1$btValidarUsuario'] = 'Entrar'
            r = self.s.post(r.url, data=data, timeout=30)
            r.raise_for_status()
        if not r.url.endswith('/Citas.aspx'):
            raise ImedError('Login fallido: revisa usuario y contraseña')

    def _get(self, url):
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        if url == CITAS and 'login' in r.url.lower():
            raise ImedError('Sesión caducada')
        return r

    def _post(self, html, extra=None, target=''):
        data = campos(html)
        data['__EVENTTARGET'] = target
        data.update(extra or {})
        r = self.s.post(CITAS, data=data, timeout=30)
        r.raise_for_status()
        if 'error500' in r.url or 'login' in r.url.lower():
            raise ImedError(f'La web devolvió {r.url}')
        return r.text

    def buscar(self, centro, especialidad, subconsulta):
        html = self._get(CITAS).text
        marcados = {k: None for k in campos(html) if 'cblCentros' in k}
        soup = BeautifulSoup(html, 'html.parser')
        check = soup.find('input', {'name': re.compile(r'cblCentros'), 'value': centro})
        if check is None:
            raise ImedError(f'No existe el centro {centro}')
        html = self._post(html, {**marcados, check['name']: centro, f'{P}bAceptarCentro': 'Aceptar'})
        html = self._post(html, {f'{P}DDLEspecialidades': especialidad}, f'{P}DDLEspecialidades')
        if subconsulta:
            html = self._post(html, {f'{P}DDLTipoConsulta': subconsulta}, f'{P}DDLTipoConsulta')
        html = self._post(html, {f'{P}bBuscar': 'Buscar'})
        if 'MainContent_Calendario' not in html:
            raise ImedError('La búsqueda no devolvió el calendario')
        return html

    def dias_libres(self, html, mes, hasta):
        """Muestra `mes` en el calendario y devuelve sus días con hueco hasta `hasta`."""
        data = campos(html)
        data[f'{P}Calendario'] = json.dumps({
            'visibleDate': mes.strftime('%m/%d/%Y'),
            'initialVisibleDate': date.today().strftime('%m/%d/%Y'),
            'selectedDates': [],
        })
        data['__CALLBACKID'] = f'{P}Calendario'
        data['__CALLBACKPARAM'] = 'c0:UPDATE'
        r = self.s.post(CITAS, data=data, timeout=30)
        r.raise_for_status()
        if '/*DX*/' not in r.text:
            raise ImedError('Respuesta inesperada del calendario')
        return [d for d in dict.fromkeys(RE_DIA.findall(r.text))
                if datetime.strptime(d, '%d/%m/%Y').date() <= hasta]

    def horas(self, html, dia):
        html = self._post(html, {f'{P}hfFecha': f'{dia} 0:00:00', f'{P}bCitaDia': ''})
        soup = BeautifulSoup(html, 'html.parser')
        res = []
        for td in soup.select('table.tablaItems td.tablaTd'):
            hora = td.find('input', {'name': re.compile(r'bFechaHueco$')})
            medico = td.find('input', {'name': re.compile(r'bProfesionalHueco$')})
            if hora:
                res.append((hora['value'], medico['value'] if medico else ''))
        return res


def huecos(imed, especialidad, subconsulta, hasta):
    """Devuelve {clave: texto} con un elemento por hueco (o por día si no se piden horas)."""
    res = {}
    for centro, nombre in CENTROS.items():
        html = imed.buscar(centro, especialidad, subconsulta)
        mes = date.today().replace(day=1)
        n = 0
        while mes <= hasta:
            # Las horas se piden justo después de mostrar su mes: el servidor responde
            # sin horas para los días de un mes distinto al último mostrado.
            for dia in imed.dias_libres(html, mes, hasta):
                n += 1
                if n > MAX_DIAS_CON_HORAS:
                    res[f'{centro}|{dia}'] = f'{dia} ({nombre})'
                    continue
                horas = imed.horas(html, dia)
                # Pasada la medianoche el calendario marca el día actual en todos los centros
                # aunque no tenga horas: no es un hueco real.
                if not horas:
                    print(f'{nombre}: {dia} sin horas, se ignora')
                for hora, medico in horas:
                    res[f'{centro}|{dia}|{hora}|{medico}'] = f'{dia} {hora} · {medico} ({nombre})'
            mes = (mes + timedelta(days=31)).replace(day=1)
        print(f'{nombre}: {n} días con hueco')
    return res


def notificar(topic, nuevos):
    lineas = sorted(nuevos, key=lambda t: (t[6:10], t[3:5], t[:2], t))
    if len(lineas) > 20:
        lineas = lineas[:20] + [f'… y {len(lineas) - 20} más']
    r = requests.post('https://ntfy.sh/', timeout=30, json={
        'topic': topic,
        'title': f'{len(nuevos)} hueco(s) disponible(s)',
        'message': '\n'.join(lineas),
        'priority': 5,
        'tags': ['bell'],
        'click': CITAS,
    })
    r.raise_for_status()


def revisar(imed, args):
    actuales = huecos(imed, args.especialidad, args.subconsulta,
                      date.today() + timedelta(days=args.dias))
    try:
        avisados = set(json.loads(ESTADO.read_text(encoding='utf-8')))
    except FileNotFoundError:
        avisados = set()
    nuevos = [txt for clave, txt in actuales.items() if clave not in avisados]
    print(f'{len(actuales)} huecos, {len(nuevos)} nuevos')
    if args.sin_aviso:
        print('\n'.join(nuevos))
        return
    if nuevos:
        notificar(os.environ['NTFY_TOPIC'], nuevos)
    # Se guarda solo lo vigente: si un hueco desaparece y vuelve a salir, se avisa otra vez.
    ESTADO.write_text(json.dumps(sorted(actuales)), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dias', type=int, default=30, help='horizonte de búsqueda desde hoy')
    ap.add_argument('--especialidad', default=ESPECIALIDAD)
    ap.add_argument('--subconsulta', default=SUBCONSULTA, help="'' si la especialidad no tiene")
    ap.add_argument('--sin-aviso', action='store_true', help='solo muestra, no notifica ni guarda')
    ap.add_argument('--minutos', type=float, default=0,
                    help='seguir revisando durante este tiempo (0 = una sola revisión)')
    ap.add_argument('--intervalo', type=float, default=25, help='minutos entre revisiones')
    args = ap.parse_args()

    fin = time.monotonic() + args.minutos * 60
    imed = None
    vueltas = fallos = 0
    while True:
        vueltas += 1
        print(time.strftime('%H:%M'), 'revisando')
        # Un segundo intento con login nuevo cubre la sesión caducada y los cortes puntuales.
        for _ in range(2):
            try:
                if imed is None:
                    imed = Imed(os.environ['IMED_USUARIO'], os.environ['IMED_PASSWORD'])
                revisar(imed, args)
                break
            except (ImedError, requests.RequestException) as e:
                print(f'Error: {e}')
                imed = None
        else:
            fallos += 1
        if time.monotonic() >= fin:
            break
        # También se espera tras la última revisión: la ejecución siguiente (en cola)
        # arranca al terminar esta y así se mantiene el intervalo entre ambas.
        time.sleep(args.intervalo * 60)
        if time.monotonic() >= fin:
            break
    if fallos == vueltas:
        sys.exit('Todas las revisiones han fallado')


if __name__ == '__main__':
    main()
