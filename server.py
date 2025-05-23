import json
import sys
from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from functools import lru_cache
import requests
import math

# Intentar importar Skyfield y módulos necesarios
try:
    from skyfield.api import load, wgs84
    import numpy as np
    SKYFIELD_AVAILABLE = True
    print("Skyfield disponible para cálculos astronómicos precisos")
except ImportError:
    SKYFIELD_AVAILABLE = False
    print("ADVERTENCIA: Skyfield no está instalado. El servidor puede no funcionar correctamente.")

# Intentar importar xml.etree para el intérprete
try:
    import xml.etree.ElementTree as ET
    XML_AVAILABLE = True
except ImportError:
    XML_AVAILABLE = False
    print("ADVERTENCIA: xml.etree no está disponible. Las interpretaciones no funcionarán.")

# Intentar importar CSV para zonas horarias
try:
    import csv
    CSV_AVAILABLE = True
except ImportError:
    CSV_AVAILABLE = False
    print("ADVERTENCIA: csv no está disponible. Se usarán zonas horarias aproximadas.")

try:
    import swisseph as swe
    SWISSEPH_AVAILABLE = True
    print("Swiss Ephemeris disponible para cálculos de casas precisos")
except ImportError:
    SWISSEPH_AVAILABLE = False
    print("NOTA: Swiss Ephemeris no disponible - usando cálculos aproximados")
    print("Para máxima precisión, instala pyswisseph (requiere Visual C++ Build Tools)")

app = Flask(__name__)
# Configurar CORS correctamente
CORS(app, resources={r"/*": {"origins": "*"}})

# Variables globales para recursos precargados
eph = None
ts = None
interpreter = None
time_zone_df = None

API_KEY = "e19afa2a9d6643ea9550aab89eefce0b"

# Constantes para cálculos astrológicos
PLANET_DATA = {
    'SOL': {'numero': 1},
    'LUNA': {'numero': 6},
    'MERCURIO': {'numero': 4},
    'VENUS': {'numero': 3},
    'MARTE': {'numero': 5},
    'JÚPITER': {'numero': 2},
    'SATURNO': {'numero': 7}
}

# Función para calcular dinámicamente el Ayanamsa Fagan-Allen
def calculate_fagan_allen_ayanamsa(date):
    """
    Calcula el Ayanamsa Fagan-Allen para una fecha dada.
    date: datetime objeto
    """
    # Fecha J2000.0 estándar: 1 enero 2000, 12:00 UTC
    j2000 = datetime(2000, 1, 1, 12, 0, tzinfo=timezone.utc)
    # Ayanamsa Fagan-Allen en J2000.0 era aproximadamente 24°
    ayanamsa_j2000 = 24.0
    
    # Calcular años desde J2000.0
    if not date.tzinfo:
        date = date.replace(tzinfo=timezone.utc)
    years_since_j2000 = (date - j2000).total_seconds() / (365.25 * 24 * 60 * 60)
    
    # La precesión avanza aprox. 50.3 segundos de arco por año
    # que es igual a 50.3/3600 = 0.01397 grados por año
    precession_rate = 0.01397
    
    # Ayanamsa actual = Ayanamsa en J2000 + la precesión acumulada
    return ayanamsa_j2000 + (precession_rate * years_since_j2000)

def calculate_true_lunar_node(t, eph):
    """
    Calcula el nodo lunar verdadero usando Skyfield de manera simplificada
    """
    try:
        # Si tienes pyswisseph disponible (más fácil)
        if 'swe' in sys.modules:
            import swisseph as swe
            jd = t.tt
            result = swe.calc_ut(jd, swe.TRUE_NODE)
            return result[0]
        
        # Método simplificado con Skyfield
        earth = eph['earth']
        moon = eph['moon']
        
        # Obtener posiciones en varios momentos para determinar cuando la latitud lunar cruza por cero
        # Esto es una aproximación simplificada
        positions = []
        
        # Obtener 5 posiciones a lo largo de 2 días
        for offset in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            t_offset = ts.tt_jd(t.tt + offset)
            pos = earth.at(t_offset).observe(moon).apparent()
            lat, lon, _ = pos.ecliptic_latlon()
            positions.append((t_offset.tt, float(lat.degrees), float(lon.degrees)))
        
        # Buscar dónde la latitud cambia de signo (cruza el ecuador)
        for i in range(len(positions) - 1):
            if positions[i][1] * positions[i+1][1] <= 0:  # Cambio de signo o cero
                # Encontramos un cruce, ahora determinamos si es ascendente o descendente
                if positions[i][1] < positions[i+1][1]:
                    # Cruce ascendente (de sur a norte)
                    node_lon = positions[i][2]
                else:
                    # Cruce descendente (de norte a sur), añadir 180°
                    node_lon = (positions[i][2] + 180) % 360
                
                return node_lon
        
        # Si no encontramos un cruce, usamos una aproximación basada en el nodo medio
        jd = t.tt
        days_since_j2000 = jd - 2451545.0
        return (125.044522 - 0.0529538083 * days_since_j2000) % 360
    
    except Exception as e:
        print(f"Error calculando nodo lunar: {str(e)}")
        # Valor aproximado como fallback
        jd = t.tt
        days_since_j2000 = jd - 2451545.0
        return (125.044522 - 0.0529538083 * days_since_j2000) % 360

# Dignidades planetarias
DIGNIDADES = {
    'SOL': {
        'domicilio': ['ESCORPIO', 'GÉMINIS', 'PEGASO'], 
        'exaltacion': ['LEO', 'ARIES', 'CAPRICORNIO', 'VIRGO'], 
        'caida': ['CÁNCER', 'PISCIS', 'LIBRA', 'ACUARIO', 'OFIUCO'], 
        'exilio': ['TAURO', 'SAGITARIO']
    },
    'LUNA': {
        'domicilio': ['TAURO', 'SAGITARIO'], 
        'exaltacion': ['CÁNCER', 'PISCIS', 'LIBRA', 'ACUARIO', 'OFIUCO'], 
        'caida': ['LEO', 'ARIES', 'CAPRICORNIO', 'VIRGO'], 
        'exilio': ['ESCORPIO', 'GÉMINIS', 'PEGASO']
    },
    'MERCURIO': {
        'domicilio': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO'], 
        'exaltacion': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO'], 
        'caida': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO'], 
        'exilio': ['CÁNCER', 'PISCIS', 'SAGITARIO']
    },
    'VENUS': {
        'domicilio': ['CÁNCER', 'PISCIS', 'SAGITARIO'], 
        'exaltacion': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO'], 
        'caida': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO'], 
        'exilio': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO']
    },
    'MARTE': {
        'domicilio': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO'], 
        'exaltacion': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO'], 
        'caida': ['CÁNCER', 'PISCIS', 'SAGITARIO'], 
        'exilio': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO']
    },
    'JÚPITER': {
        'domicilio': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO'], 
        'exaltacion': ['CÁNCER', 'PISCIS', 'SAGITARIO'], 
        'caida': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO'], 
        'exilio': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO']
    },
    'SATURNO': {
        'domicilio': ['LEO', 'ARIES', 'LIBRA', 'ACUARIO'], 
        'exaltacion': ['OFIUCO', 'GÉMINIS', 'SAGITARIO'], 
        'caida': ['TAURO', 'ESCORPIO', 'PEGASO'], 
        'exilio': ['CÁNCER', 'PISCIS', 'CAPRICORNIO', 'VIRGO']
    },
    'URANO': {
        'domicilio': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO'], 
        'exaltacion': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO'], 
        'caida': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO'],
        'exilio': ['CÁNCER', 'PISCIS', 'SAGITARIO']
    },
    'NEPTUNO': {
        'domicilio': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO'], 
        'exaltacion': ['CÁNCER', 'PISCIS', 'SAGITARIO'], 
        'caida': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO'], 
        'exilio': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO']
    },
    'PLUTÓN': {
        'domicilio': ['GÉMINIS', 'CAPRICORNIO', 'VIRGO'], 
        'exaltacion': ['LEO', 'ARIES', 'ESCORPIO', 'PEGASO'], 
        'caida': ['CÁNCER', 'PISCIS', 'SAGITARIO'], 
        'exilio': ['TAURO', 'LIBRA', 'ACUARIO', 'OFIUCO']
    }
}

# Precarga de recursos al inicio
def preload_resources():
    # Solo cargar recursos si Skyfield está disponible
    if not SKYFIELD_AVAILABLE:
        print("Skyfield no está disponible. No se pueden cargar efemérides.")
        return
        
    global eph, ts, interpreter, time_zone_df
    
    print("Precargando recursos...")
    
    # Cargar efemérides
    try:
        # Cargar desde archivo local
        eph_path = Path('de421.bsp')
        if not eph_path.exists():
            # Intentar cargar desde la carpeta docs
            eph_path = Path('docs') / 'de421.bsp'
        
        print(f"Cargando efemérides desde: {eph_path}")
        eph = load(str(eph_path))
    except Exception as e:
        print(f"Error cargando efemérides: {e}")
        # Intento alternativo
        try:
            print("Intentando cargar efemérides alternativas...")
            eph = load('de440s.bsp')
        except Exception as e2:
            print(f"Error en carga alternativa: {e2}")
            print("ADVERTENCIA: No se pudieron cargar efemérides. Los cálculos astrológicos pueden no ser precisos.")
    
    ts = load.timescale()
    
    # Cargar zona horaria desde CSV si está disponible
    if CSV_AVAILABLE:
        try:
            time_zone_df = []
            csv_paths = ['time_zone.csv', 'docs/time_zone.csv', '../time_zone.csv']
            
            for csv_path in csv_paths:
                if os.path.exists(csv_path):
                    with open(csv_path, 'r') as csv_file:
                        csv_reader = csv.reader(csv_file)
                        for row in csv_reader:
                            if len(row) >= 6:  # asegurarse de que hay suficientes columnas
                                time_zone_df.append({
                                    'timezone': row[0],
                                    'country_code': row[1],
                                    'abbreviation': row[2],
                                    'timestamp': int(row[3]) if row[3].isdigit() else 0,
                                    'utc_offset': float(row[4]) if row[4].replace('.', '', 1).isdigit() else 0,
                                    'dst': int(row[5]) if row[5].isdigit() else 0
                                })
                    print(f"Cargado archivo de zonas horarias: {len(time_zone_df)} entradas")
                    break
            
            if not time_zone_df:
                print("No se encontró archivo de zonas horarias")
        except Exception as e:
            print(f"Error cargando zonas horarias: {e}")
            time_zone_df = []
    
    # Cargar intérprete con XML si está disponible
    if XML_AVAILABLE:
        try:
            interpreter = AstrologicalInterpreter()
            print("Intérprete astrológico cargado")
        except Exception as e:
            print(f"Error cargando intérprete: {e}")
            interpreter = None
    
    print("Recursos precargados correctamente")

class AstrologicalInterpreter:
    def __init__(self, xml_path='interpretations.xml'):
        # Buscar en diferentes ubicaciones posibles
        paths = [xml_path, 'docs/interpretations.xml', '../interpretations.xml']
        found_path = None
        
        for path in paths:
            if os.path.exists(path):
                found_path = path
                break
        
        if not found_path:
            print(f"No se encontró archivo de interpretaciones")
            self.root = None
            return
            
        try:
            self.tree = ET.parse(found_path)
            self.root = self.tree.getroot()
            print(f"XML de interpretaciones cargado desde {found_path}")
        except Exception as e:
            print(f"Error al cargar el archivo XML: {e}")
            # No lanzar excepción, solo reportar el error
            self.root = None

    def get_planet_in_sign(self, planet, sign):
        try:
            if not self.root:
                return None
                
            xpath = f".//PLANET_IN_SIGN14/{planet}/{sign}"
            planet_element = self.root.find(xpath)
            
            if planet_element is not None:
                full_text = planet_element.text.strip() if planet_element.text else ""
                physical_desc = ""
                astral_desc = ""
                
                split_text = full_text.split("En el plano Astral", 1)
                
                if len(split_text) > 0:
                    physical_desc = split_text[0].strip()
                if len(split_text) > 1:
                    astral_desc = "En el plano Astral" + split_text[1].strip()
                
                return {
                    "physical": physical_desc,
                    "astral": astral_desc
                }
            return None
        except Exception as e:
            print(f"Error en get_planet_in_sign: {e}")
            return None

    def get_planet_in_house(self, planet, house):
        try:
            if not self.root:
                return None
                
            house_str = f"HS{house}"
            xpath = f".//PLANET_IN_12HOUSE/{planet}/{house_str}"
            house_element = self.root.find(xpath)
            
            if house_element is not None and house_element.text:
                return house_element.text.strip()
            return None
        except Exception as e:
            print(f"Error en get_planet_in_house: {e}")
            return None

    def get_aspect_interpretation(self, planet1, planet2, aspect_type):
        try:
            if not self.root:
                return None
                
            aspect_angles = {
                "Armónico Relevante": ["0", "60", "120", "180"],
                "Inarmónico Relevante": ["90", "150"],
                "Armónico": ["12", "24", "36", "48", "72", "84", "96", "108", "132", "144", "156", "168"],
                "Inarmónico": ["6", "18", "42", "54", "66", "78", "102", "114", "126", "138", "162", "174"]
            }
            
            for angles in aspect_angles[aspect_type]:
                xpath = f".//PLANET_IN_ASPECT/{planet1}/{planet2}/ASP_{angles}"
                aspect_element = self.root.find(xpath)
                
                if aspect_element is not None and aspect_element.text:
                    return aspect_element.text.strip()
                
                xpath = f".//PLANET_IN_ASPECT/{planet2}/{planet1}/ASP_{angles}"
                aspect_element = self.root.find(xpath)
                
                if aspect_element is not None and aspect_element.text:
                    return aspect_element.text.strip()
            
            return None
        except Exception as e:
            print(f"Error en get_aspect_interpretation: {e}")
            return None

    def get_house_ruler_interpretation(self, ruler_house, house_position):
        try:
            if not self.root:
                return None
                
            xpath = f".//HRULER_IN_HOUSE/RH{ruler_house}/HS{house_position}"
            ruler_element = self.root.find(xpath)
            
            if ruler_element is not None and ruler_element.text:
                return ruler_element.text.strip()
            return None
        except Exception as e:
            print(f"Error en get_house_ruler_interpretation: {e}")
            return None

# Cachear obtención de datos de ciudad
@lru_cache(maxsize=100)
def obtener_datos_ciudad(ciudad, fecha=None, hora=None):
    url = f"https://api.geoapify.com/v1/geocode/search?text={ciudad}&apiKey={API_KEY}"
    try:
        response = requests.get(url, timeout=10)  # Timeout para evitar demoras
        if response.status_code == 200:
            datos = response.json()
            if datos.get("features"):
                opciones = [{
                    "nombre": resultado["properties"]["formatted"],
                    "lat": resultado["properties"]["lat"],
                    "lon": resultado["properties"]["lon"],
                    "pais": resultado["properties"].get("country", "")
                }
                for resultado in datos["features"]]
                return opciones
            return {"error": "Ciudad no encontrada"}
        return {"error": f"Error en la consulta: {response.status_code}"}
    except requests.exceptions.Timeout:
        return {"error": "Timeout en la consulta"}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def obtener_zona_horaria(coordenadas, fecha):
    """
    Obtiene la zona horaria usando el archivo time_zone.csv y ajusta para horario de verano/invierno
    basado en las coordenadas y la fecha, considerando hemisferio norte/sur
    """
    try:
        lat = coordenadas["lat"]
        lon = coordenadas["lon"]
        fecha_obj = datetime.strptime(fecha, "%Y-%m-%d")
        
        # Determinar hemisferio (norte o sur)
        hemisferio = "norte" if lat >= 0 else "sur"
        
        # Verificar si la fecha está en horario de verano
        # Esta función necesita ser más precisa para fechas históricas
        is_dst = determinar_horario_verano(fecha_obj, hemisferio, coordenadas)
        
        # Buscar en el CSV por aproximación de longitud
        estimated_offset = round(lon / 15)
        
        # Ajustar para países específicos con información conocida
        pais = coordenadas.get("pais", "").lower()
        abbr = "UTC"
        tz_name = "UTC"
        offset = estimated_offset  # valor por defecto
        
        if "spain" in pais or "españa" in pais:
            tz_name = "Europe/Madrid"
            abbr = "CET"
            abbr_dst = "CEST"
            offset = 1
            if is_dst:
                offset = 2
                abbr = abbr_dst
        elif "argentina" in pais:
            tz_name = "America/Argentina/Buenos_Aires"
            abbr = "ART"
            offset = -3
            # Argentina no usa DST actualmente
        elif "mexico" in pais or "méxico" in pais:
            tz_name = "America/Mexico_City"
            abbr = "CST"
            abbr_dst = "CDT"
            offset = -6
            if is_dst:
                offset = -5
                abbr = abbr_dst
        else:
            # Buscar en el CSV la zona más cercana a la longitud estimada
            closest_zone = None
            min_diff = float('inf')
            
            if time_zone_df:
                for zone in time_zone_df:
                    # Los offsets en el CSV están en segundos, convertir a horas
                    csv_offset = zone['utc_offset'] / 3600
                    diff = abs(csv_offset - estimated_offset)
                    
                    if diff < min_diff:
                        min_diff = diff
                        closest_zone = zone
                
                if closest_zone:
                    offset = closest_zone['utc_offset'] / 3600
                    abbr = closest_zone['abbreviation']
                    tz_name = closest_zone['timezone']
                    
                    # Ajustar por DST si corresponde
                    if is_dst and closest_zone['dst'] == 1:
                        offset += 1
            else:
                # Si no hay datos en el CSV, usar la estimación por longitud
                offset = estimated_offset
                abbr = f"GMT{offset:+d}"
                tz_name = f"Estimated/GMT{offset:+d}"
        
        print(f"Zona horaria determinada: {tz_name}, offset: {offset}, DST: {is_dst}")
        
        return {
            "name": tz_name,
            "offset": offset,
            "abbreviation_STD": abbr,
            "abbreviation_DST": abbr,
            "is_dst": is_dst,
            "hemisphere": hemisferio
        }
    
    except Exception as e:
        print(f"Error obteniendo zona horaria: {str(e)}")
        # Si hay un error, devolver un mensaje claro
        print("Error en obtención de zona horaria, usando estimación basada en longitud")
        
        try:
            # Estimar zona horaria basada en longitud
            lon = coordenadas["lon"]
            estimated_offset = round(lon / 15)  # 15 grados = 1 hora
            
            # Para ciudades conocidas, usar valores predeterminados
            pais = coordenadas.get("pais", "").lower()
            
            if "spain" in pais or "españa" in pais:
                estimated_offset = 1
            elif "argentina" in pais:
                estimated_offset = -3
            elif "mexico" in pais or "méxico" in pais:
                estimated_offset = -6
            elif "united states" in pais or "estados unidos" in pais:
                # Aproximación basada en longitud para EEUU
                if lon < -100:
                    estimated_offset = -8  # Pacífico
                elif lon < -90:
                    estimated_offset = -7  # Montaña
                elif lon < -75:
                    estimated_offset = -6  # Central
                else:
                    estimated_offset = -5  # Este
            
            return {
                "name": f"GMT{estimated_offset:+d}",
                "offset": estimated_offset,
                "abbreviation_STD": f"GMT{estimated_offset:+d}",
                "abbreviation_DST": f"GMT{estimated_offset:+d}",
                "is_dst": False,
                "hemisphere": "norte" if coordenadas["lat"] >= 0 else "sur",
                "lon": lon  # Añadir longitud para referencia
            }
        except Exception as inner_e:
            print(f"Error en estimación de zona horaria: {str(inner_e)}")
            # Valor por defecto UTC si todo falla
            return {
                "name": "UTC",
                "offset": 0,
                "abbreviation_STD": "UTC",
                "abbreviation_DST": "UTC",
                "is_dst": False,
                "hemisphere": "norte",
                "estimated": True
            }

def determinar_horario_verano(fecha, hemisferio, coordenadas):
    """
    Determina si una fecha está en horario de verano (DST)
    Basado en reglas históricas y específicas por país
    """
    año = fecha.year
    mes = fecha.month
    dia = fecha.day
    
    # Obtener país
    pais = coordenadas.get("pais", "").lower()
    
    # Reglas específicas para España
    if "spain" in pais or "españa" in pais:
        # España antes de 1974: no había DST
        if año < 1974:
            return False
        elif año >= 1974 and año <= 1975:
            # En 1974-1975, DST fue del 13 de abril al 6 de octubre
            if (mes > 4 and mes < 10) or (mes == 4 and dia >= 13) or (mes == 10 and dia <= 6):
                return True
            return False
        elif año >= 1976 and año <= 1996:
            # Reglas más genéricas para 1976-1996
            # Primavera a otoño, aproximadamente marzo/abril a septiembre/octubre
            if mes > 3 and mes < 10:
                return True
            return False
        else:
            # Desde 1997: Regla actual de la UE - último domingo de marzo a último domingo de octubre
            if mes > 3 and mes < 10:
                return True
            # Marzo: último domingo
            elif mes == 3 and dia >= 25:  # Aproximación al último domingo
                return True
            # Octubre: último domingo
            elif mes == 10 and dia <= 25:  # Aproximación al último domingo
                return True
            return False
    
    # Reglas para otros países
    # Hemisferio Norte (Europa, América del Norte, Asia)
    elif hemisferio == "norte":
        # La mayoría de los países del hemisferio norte siguen este patrón
        # Horario de verano: finales de marzo a finales de octubre
        if año < 1970:
            # Antes de 1970 era menos común el DST
            return False
        
        if mes > 3 and mes < 10:
            return True
        elif mes == 3 and dia >= 25:  # Aproximación al último domingo de marzo
            return True
        elif mes == 10 and dia <= 25:  # Aproximación al último domingo de octubre
            return True
        return False
    
    # Hemisferio Sur (Australia, Nueva Zelanda, Sudamérica)
    else:
        # Muchos países del hemisferio sur no utilizan DST
        # Algunos que sí lo utilizan: Australia, Nueva Zelanda, Chile, Paraguay
        
        # Lista de países conocidos del hemisferio sur con DST
        south_dst_countries = ["australia", "new zealand", "nueva zelanda", "chile", "paraguay"]
        
        # Si no está en la lista, asumimos que no usa DST
        pais_usa_dst = any(country in pais for country in south_dst_countries)
        if not pais_usa_dst:
            return False
            
        # Horario de verano: finales de octubre a finales de marzo
        if mes < 3 or mes > 10:
            return True
        elif mes == 3 and dia <= 25:  # Aproximación al último domingo de marzo
            return True
        elif mes == 10 and dia >= 25:  # Aproximación al último domingo de octubre
            return True
        return False

def convertir_a_utc(fecha, hora, timezone_info):
    """
    Convierte fecha y hora local a UTC considerando zona horaria y DST
    Para cálculos astrológicos correctos, debemos asegurarnos de que la hora UTC sea precisa
    """
    try:
        # Combinar fecha y hora en un objeto datetime
        fecha_hora_str = f"{fecha} {hora}"
        dt_local = datetime.strptime(fecha_hora_str, "%Y-%m-%d %H:%M")
        
        # Obtener offset en horas desde la API de zona horaria
        # Si estamos en DST, la API ya incluye ese offset
        offset_hours = timezone_info["offset"]
        
        print(f"Offset de zona horaria: {offset_hours} horas")
        print(f"Hora local ingresada: {dt_local}")
        
        # Crear un timezone con el offset
        tz = timezone(timedelta(hours=offset_hours))
        
        # Aplicar timezone al datetime
        dt_local_with_tz = dt_local.replace(tzinfo=tz)
        
        # Convertir a UTC
        dt_utc = dt_local_with_tz.astimezone(timezone.utc)
        
        print(f"Hora convertida a UTC: {dt_utc}")
        return dt_utc
    except Exception as e:
        print(f"Error en conversión a UTC: {str(e)}")
        # Si falla, usar la hora proporcionada con offset manual aproximado
        dt_local = datetime.strptime(f"{fecha} {hora}", "%Y-%m-%d %H:%M")
        
        # Intentar estimar offset basado en longitud si no tenemos zona horaria
        if "lon" in timezone_info:
            lon = timezone_info["lon"]
            est_offset = round(lon / 15)  # 15 grados = 1 hora
            est_tz = timezone(timedelta(hours=est_offset))
            dt_with_tz = dt_local.replace(tzinfo=est_tz)
            return dt_with_tz.astimezone(timezone.utc)
        
        # Fallback: asumir UTC
        return dt_local.replace(tzinfo=timezone.utc)

def calculate_positions_with_utc(utc_datetime, lat=None, lon=None, use_sidereal=False):
    """
    Calcula posiciones planetarias con un datetime UTC
    Asegura que el tiempo se ajusta correctamente según la zona horaria
    """
    if not SKYFIELD_AVAILABLE:
        print("ADVERTENCIA: Skyfield no está disponible, usando cálculos simulados")
        return calculate_positions_simulated(utc_datetime, lat, lon, use_sidereal)
    
    try:
        # Usar el datetime UTC directamente
        print(f"Calculando posiciones para UTC: {utc_datetime}")
        t = ts.from_datetime(utc_datetime)
        
        # Para calcular movimientos retrógrados, necesitamos puntos adyacentes en el tiempo
        t_before = ts.from_datetime(utc_datetime - timedelta(days=1))  # Un día antes
        t_after = ts.from_datetime(utc_datetime + timedelta(days=1))   # Un día después
        
        earth = eph['earth']
        
        positions = []
        bodies = {
            'SOL': eph['sun'],
            'LUNA': eph['moon'],
            'MERCURIO': eph['mercury'],
            'VENUS': eph['venus'],
            'MARTE': eph['mars'],
            'JÚPITER': eph['jupiter barycenter'],
            'SATURNO': eph['saturn barycenter'],
            'URANO': eph['uranus barycenter'],
            'NEPTUNO': eph['neptune barycenter'],
            'PLUTÓN': eph['pluto barycenter']
        }
        
        for body_name, body in bodies.items():
            # Posición actual
            pos = earth.at(t).observe(body).apparent()
            lat_ecl, lon_ecl, dist = pos.ecliptic_latlon(epoch='date')
            
            # Obtener longitud tropical
            longitude = float(lon_ecl.degrees) % 360
            
            # Si se requiere zodiaco sideral, aplicar corrección
            if use_sidereal:
                # Calcular Ayanamsa Fagan-Allen para la fecha
                ayanamsa = calculate_fagan_allen_ayanamsa(utc_datetime)
                # Guardar la longitud tropical
                tropical_longitude = longitude
                # Ajustar por el ayanamsa
                longitude = (longitude - ayanamsa + 360) % 360
            
            # Calcular posiciones para determinar el movimiento
            pos_before = earth.at(t_before).observe(body).apparent()
            lat_before, lon_before, dist_before = pos_before.ecliptic_latlon(epoch='date')
            longitude_before = float(lon_before.degrees) % 360
            
            if use_sidereal:
                # Aplicar corrección sideral
                longitude_before = (longitude_before - ayanamsa + 360) % 360
            
            pos_after = earth.at(t_after).observe(body).apparent()
            lat_after, lon_after, dist_after = pos_after.ecliptic_latlon(epoch='date')
            longitude_after = float(lon_after.degrees) % 360
            
            if use_sidereal:
                # Aplicar corrección sideral
                longitude_after = (longitude_after - ayanamsa + 360) % 360
            
            # Calcular movimiento diario
            daily_motion_before = (longitude - longitude_before) % 360
            if daily_motion_before > 180:
                daily_motion_before = daily_motion_before - 360
                
            daily_motion_after = (longitude_after - longitude) % 360
            if daily_motion_after > 180:
                daily_motion_after = daily_motion_after - 360
            
            # Determinar estado retrógrado
            motion_status = "direct"  # Valor por defecto
            
            # El Sol y la Luna nunca son retrógrados geocéntricamente
            if body_name not in ['SOL', 'LUNA']:
                # Comprobar movimiento retrógrado (movimiento diario negativo)
                if daily_motion_before < 0 and daily_motion_after < 0:
                    motion_status = "retrograde"
                # Comprobar estacionario retrógrado (cambiando de directo a retrógrado)
                elif daily_motion_before >= 0 and daily_motion_after < 0:
                    motion_status = "stationary_retrograde"
                # Comprobar estacionario directo (cambiando de retrógrado a directo)
                elif daily_motion_before < 0 and daily_motion_after >= 0:
                    motion_status = "stationary_direct"
                
                # Comprobar movimiento muy lento (casi estacionario)
                if abs(daily_motion_before) < 0.1 or abs(daily_motion_after) < 0.1:
                    if motion_status == "retrograde":
                        motion_status = "stationary_retrograde"
                    elif motion_status == "direct":
                        motion_status = "stationary_direct"
            
            # Obtener el signo
            sign = get_sign(longitude)
            
            # Calcular dignidad basada en el signo
            dignidad = calcular_dignidad_planetaria(body_name, sign)
            
            # Crear el objeto con los datos del planeta
            planet_data = {
                "name": body_name,
                "longitude": longitude,
                "sign": sign,
                "dignidad": dignidad,
                "motion_status": motion_status,
                "daily_motion": (daily_motion_before + daily_motion_after) / 2  # Movimiento diario promedio
            }
            
            # Añadir longitud tropical si se está usando el zodiaco sideral
            if use_sidereal:
                planet_data["tropical_longitude"] = tropical_longitude
                planet_data["tropical_sign"] = get_sign(tropical_longitude)
            
            positions.append(planet_data)
        
        if lat is not None and lon is not None:
            asc, mc = calculate_asc_mc(t, lat, lon)
            
            # Ajustar ASC y MC si se usa sideral
            if use_sidereal:
                # Calcular Ayanamsa para la fecha
                ayanamsa = calculate_fagan_allen_ayanamsa(utc_datetime)
                tropical_asc = asc
                tropical_mc = mc
                asc = (asc - ayanamsa + 360) % 360
                mc = (mc - ayanamsa + 360) % 360
            
            positions.append({
                "name": "ASC",
                "longitude": float(asc),
                "sign": get_sign(asc),
                "motion_status": "direct", # ASC siempre es directo
                **({"tropical_longitude": tropical_asc, "tropical_sign": get_sign(tropical_asc)} if use_sidereal else {})
            })
            
            positions.append({
                "name": "MC",
                "longitude": float(mc),
                "sign": get_sign(mc),
                "motion_status": "direct", # MC siempre es directo
                **({"tropical_longitude": tropical_mc, "tropical_sign": get_sign(tropical_mc)} if use_sidereal else {})
            })
            
            # Añadir DSC e IC
            positions.append({
                "name": "DSC",
                "longitude": float((asc + 180) % 360),
                "sign": get_sign((asc + 180) % 360),
                "motion_status": "direct",
                **({"tropical_longitude": (tropical_asc + 180) % 360, "tropical_sign": get_sign((tropical_asc + 180) % 360)} if use_sidereal else {})
            })
            
            positions.append({
                "name": "IC",
                "longitude": float((mc + 180) % 360),
                "sign": get_sign((mc + 180) % 360),
                "motion_status": "direct",
                **({"tropical_longitude": (tropical_mc + 180) % 360, "tropical_sign": get_sign((tropical_mc + 180) % 360)} if use_sidereal else {})
            })
            
            # Calcular y añadir Parte de Fortuna y Parte de Espíritu
            sol_planet = next((p for p in positions if p["name"] == "SOL"), None)
            luna_planet = next((p for p in positions if p["name"] == "LUNA"), None)
            
            if sol_planet and luna_planet:
                # Determinar si es un nacimiento seco o húmedo
                is_dry = is_dry_birth(sol_planet["longitude"], asc)
                
                # Cálculo correcto de Parte de Fortuna basado en la naturaleza de la carta
                if is_dry:  # Carta seca (diurna)
                    # Para carta diurna: Parte de Fortuna = ASC + dist(Sol→Luna)
                    dist_sol_a_luna = (luna_planet["longitude"] - sol_planet["longitude"]) % 360
                    parte_fortuna = (asc + dist_sol_a_luna) % 360
                else:  # Carta húmeda (nocturna)
                    # Para carta nocturna: Parte de Fortuna = ASC + dist(Luna→Sol)
                    dist_luna_a_sol = (sol_planet["longitude"] - luna_planet["longitude"]) % 360
                    parte_fortuna = (asc + dist_luna_a_sol) % 360
                
                # Cálculo correcto de Parte de Espíritu (inverso lógico de Parte de Fortuna)
                if is_dry:  # Carta seca (diurna)
                    # Para carta diurna: Parte de Espíritu = ASC + dist(Luna→Sol)
                    dist_luna_a_sol = (sol_planet["longitude"] - luna_planet["longitude"]) % 360
                    parte_espiritu = (asc + dist_luna_a_sol) % 360
                else:  # Carta húmeda (nocturna)
                    # Para carta nocturna: Parte de Espíritu = ASC + dist(Sol→Luna)
                    dist_sol_a_luna = (luna_planet["longitude"] - sol_planet["longitude"]) % 360
                    parte_espiritu = (asc + dist_sol_a_luna) % 360
                
                # Añadir a posiciones
                positions.append({
                    "name": "PARTE_FORTUNA",
                    "longitude": float(parte_fortuna),
                    "sign": get_sign(parte_fortuna),
                    "motion_status": "direct"
                })
                
                positions.append({
                    "name": "PARTE_ESPIRITU",
                    "longitude": float(parte_espiritu),
                    "sign": get_sign(parte_espiritu),
                    "motion_status": "direct"
                })
                
            # Calcular nodo lunar verdadero
            true_node = calculate_true_lunar_node(t, eph)

            # Añadir nodo norte
            positions.append({
                "name": "NODO_LUNAR",
                "longitude": float(true_node),
                "sign": get_sign(true_node),
                "motion_status": "retrograde"  # Los nodos son normalmente retrógrados
            })

            # Añadir nodo sur (opcional)
            node_south = (true_node + 180) % 360
            positions.append({
                "name": "NODO_SUR",
                "longitude": float(node_south),
                "sign": get_sign(node_south),
                "motion_status": "retrograde"
            })

            # Si estás usando zodiaco sideral, ajustar también los nodos
            if use_sidereal:
                # Buscar los nodos que acabamos de añadir
                for i in range(len(positions)):
                    if positions[i]["name"] in ["NODO_LUNAR", "NODO_SUR"]:
                        tropical_longitude = positions[i]["longitude"]
                        positions[i]["tropical_longitude"] = tropical_longitude
                        positions[i]["tropical_sign"] = positions[i]["sign"]
                        positions[i]["longitude"] = (tropical_longitude - ayanamsa + 360) % 360
                        positions[i]["sign"] = get_sign(positions[i]["longitude"])
        
        return positions
    except Exception as e:
        print(f"Error calculando posiciones con Skyfield: {str(e)}")
        # Si falla, usar cálculos aproximados como respaldo
        return calculate_positions_simulated(utc_datetime, lat, lon, use_sidereal)

# Reemplazar la función calculate_asc_mc existente con esta nueva versión
def calculate_asc_mc(t, lat, lon):
    """
    Calcula Ascendente y Medio Cielo usando Swiss Ephemeris si está disponible,
    sino usa la aproximación con Skyfield
    """
    try:
        if SWISSEPH_AVAILABLE:
            # Usar Swiss Ephemeris para cálculos precisos
            julian_day = t.tt
            houses_data = swe.houses(julian_day, lat, lon, b'A')  # Sistema Placidus
            
            # houses_data[1] contiene ASC, MC, ARMC, Vertex
            asc_mc_data = houses_data[1]
            ascendente = asc_mc_data[0]  # Ascendente
            mc = asc_mc_data[1]          # Medio Cielo
            
            print(f"Swiss Ephemeris - ASC: {ascendente:.4f}°, MC: {mc:.4f}°")
            return ascendente, mc
        else:
            # Usar el método original con Skyfield como fallback
            return calculate_asc_mc_skyfield(t, lat, lon)
            
    except Exception as e:
        print(f"Error en calculate_asc_mc con Swiss Ephemeris: {str(e)}")
        # Si falla, usar el método de Skyfield
        return calculate_asc_mc_skyfield(t, lat, lon)

def calculate_asc_mc_skyfield(t, lat, lon):
    """Método original con Skyfield como fallback"""
    try:
        # Obtener tiempo sideral en Greenwich en grados
        gst = t.gast
        # Tiempo sideral local = GST + longitud
        lst = (gst * 15 + lon) % 360
        # MC es directamente el LST
        mc = lst % 360
        
        # Calcular Ascendente
        lat_rad = np.radians(lat)
        ra_rad = np.radians(lst)
        eps_rad = np.radians(23.4367)  # Oblicuidad de la eclíptica (aprox.)
        
        # Fórmula para el Ascendente
        tan_asc = np.cos(ra_rad) / (np.sin(ra_rad) * np.cos(eps_rad) + np.tan(lat_rad) * np.sin(eps_rad))
        asc = np.degrees(np.arctan(-tan_asc))
        
        # Ajustar cuadrante
        if 0 <= lst <= 180:
            if np.cos(ra_rad) > 0:
                asc = (asc + 180) % 360
        else:
            if np.cos(ra_rad) < 0:
                asc = (asc + 180) % 360
                
        asc = asc % 360
        
        print(f"Skyfield fallback - ASC: {asc:.4f}°, MC: {mc:.4f}°")
        return asc, mc
        
    except Exception as e:
        print(f"Error en calculate_asc_mc_skyfield: {str(e)}")
        # Valores por defecto en caso de error
        return 0, 0

def calcular_dignidad_planetaria(planeta, signo):
    """Calcula la dignidad planetaria basada en el signo"""
    if planeta in DIGNIDADES:
        # Verificar domicilio
        if signo in DIGNIDADES[planeta]['domicilio']:
            return "domicilio"
        # Verificar exaltación
        elif signo in DIGNIDADES[planeta]['exaltacion']:
            return "exaltacion"
        # Verificar caída
        elif signo in DIGNIDADES[planeta]['caida']:
            return "caida"
        # Verificar exilio
        elif signo in DIGNIDADES[planeta]['exilio']:
            return "exilio"
    
    # Si no está en ninguna dignidad especial
    return "peregrino"

def get_sign(longitude):
    """Determina el signo zodiacal basado en la longitud eclíptica"""
    longitude = float(longitude) % 360
    signs = [
        ("ARIES", 354.00, 36.00),
        ("TAURO", 30.00, 30.00),
        ("GÉMINIS", 60.00, 30.00),
        ("CÁNCER", 90.00, 30.00),
        ("LEO", 120.00, 30.00),
        ("VIRGO", 150.00, 36.00),
        ("LIBRA", 186.00, 24.00),
        ("ESCORPIO", 210.00, 30.00),
        ("OFIUCO", 240.00, 12.00),
        ("SAGITARIO", 252.00, 18.00),
        ("CAPRICORNIO", 270.00, 36.00),
        ("ACUARIO", 306.00, 18.00),
        ("PEGASO", 324.00, 6.00),
        ("PISCIS", 330.00, 24.00)
    ]
    
    for name, start, length in signs:
        end = start + length
        if start <= longitude < end:
            return name
        elif start > 354.00 and (longitude >= start or longitude < (end % 360)):
            # Caso especial para Aries que cruza 0°
            return name
    
    return "ARIES"  # Valor por defecto

def get_house_number(longitude, asc_longitude):
    """Calcula la casa desde el Ascendente."""
    diff = (longitude - asc_longitude) % 360
    house = 1 + (int(diff / 30))
    if house > 12:
        house = house - 12
    return house

def is_dry_birth(sol_longitude, asc_longitude):
    """Determina si un nacimiento es seco (diurno) o húmedo (nocturno)"""
    # Es seco cuando el Sol está sobre el horizonte
    diff = (sol_longitude - asc_longitude) % 360
    house = 1 + (int(diff / 30))
    
    # Es seco (diurno) si el Sol está en las casas 7 a 12
    return 7 <= house <= 12

# Modificación para la función calculate_positions_aspects en server.py
# Busca esta función en tu archivo server.py y reemplázala con esta versión:

def calculate_positions_aspects(positions):
    """Calcula aspectos entre posiciones planetarias"""
    aspects = []
    traditional_planets = ["SOL", "LUNA", "MERCURIO", "VENUS", "MARTE", "JÚPITER", "SATURNO"]
    
    def calculate_angle(pos1, pos2):
        diff = abs(pos1 - pos2) % 360
        if diff > 180:
            diff = 360 - diff
        return diff
    
    def determine_aspect_type(angle):
        orb = 2
        
        if (abs(angle) <= orb or 
            abs(angle - 60) <= orb or 
            abs(angle - 120) <= orb or
            abs(angle - 180) <= orb):
            return "Armónico Relevante"
        elif (abs(angle - 30) <= orb or
              abs(angle - 90) <= orb or
              abs(angle - 150) <= orb):
            return "Inarmónico Relevante"
        elif any(abs(angle - a) <= orb for a in [12, 24, 36, 48, 72, 84, 96, 108, 132, 144, 156, 168]):
            return "Armónico"
        elif any(abs(angle - a) <= orb for a in [6, 18, 42, 54, 66, 78, 102, 114, 126, 138, 162, 174]):
            return "Inarmónico"
            
        return None

    # Localizar el Ascendente
    asc_position = next((p for p in positions if p["name"] == "ASC"), None)
    
    # Aspectos entre planetas tradicionales diferentes
    planet_positions = [p for p in positions if p["name"] in traditional_planets]
    for i, pos1 in enumerate(planet_positions):
        # Aspectos con otros planetas tradicionales
        for pos2 in planet_positions[i+1:]:
            angle = calculate_angle(pos1["longitude"], pos2["longitude"])
            aspect_type = determine_aspect_type(angle)
            
            if aspect_type:
                aspects.append({
                    "planet1": pos1["name"],
                    "planet2": pos2["name"],
                    "type": get_aspect_key(angle),
                    "angle": angle,
                    "color": get_aspect_color(aspect_type)
                })
        
        # Aspectos con el ASC
        if asc_position:
            angle = calculate_angle(pos1["longitude"], asc_position["longitude"])
            aspect_type = determine_aspect_type(angle)
            
            if aspect_type:
                aspects.append({
                    "planet1": pos1["name"],
                    "planet2": "ASC",
                    "type": get_aspect_key(angle),
                    "angle": angle,
                    "color": get_aspect_color(aspect_type)
                })
    
    # Verificar si hay instancias duplicadas del mismo planeta 
    # (esto podría ocurrir cuando comparamos carta natal y tránsitos)
    planet_names = [p["name"] for p in planet_positions]
    duplicate_planets = set([x for x in planet_names if planet_names.count(x) > 1])
    
    # Si hay planetas duplicados, calcular aspectos entre ellos
    if duplicate_planets:
        for planet_name in duplicate_planets:
            planet_instances = [p for p in planet_positions if p["name"] == planet_name]
            
            # Calcular aspectos entre todas las instancias del mismo planeta
            for i, instance1 in enumerate(planet_instances):
                for instance2 in planet_instances[i+1:]:
                    angle = calculate_angle(instance1["longitude"], instance2["longitude"])
                    aspect_type = determine_aspect_type(angle)
                    
                    if aspect_type:
                        aspects.append({
                            "planet1": instance1["name"],
                            "planet2": instance2["name"],
                            "type": get_aspect_key(angle),
                            "angle": angle,
                            "color": get_aspect_color(aspect_type),
                            "isSamePlanet": True,
                            # Añadir cualquier información adicional que pueda ayudar a distinguir
                            # entre las diferentes instancias del mismo planeta
                            "instance1_info": instance1.get("chart_type", "natal"),
                            "instance2_info": instance2.get("chart_type", "transit")
                        })
    
    return aspects

def get_aspect_key(angle):
    """Obtiene la clave de aspecto basada en el ángulo"""
    angle = round(angle)
    
    if angle <= 2:  # Conjunción
        return "CONJUNCTION"
    elif 58 <= angle <= 62:  # Sextil
        return "SEXTILE"
    elif 88 <= angle <= 92:  # Cuadratura
        return "SQUARE"
    elif 118 <= angle <= 122:  # Trígono
        return "TRINE"
    elif 178 <= angle <= 180:  # Oposición
        return "OPPOSITION"
    
    # Para otros aspectos, usar claves genéricas
    if angle == 6: return "SEIS"
    if angle == 12: return "DOCE"
    if angle == 18: return "DIECIOCHO"
    if angle == 24: return "VEINTICUATRO"
    if angle == 30: return "TREINTA"
    if angle == 36: return "TREINTAYSEIS"
    if angle == 42: return "CUARENTAYDOS"
    if angle == 48: return "CUARENTAYOCHO"
    if angle == 54: return "CICUENTAYCUATRO"
    if angle == 66: return "SESENTAYSEIS"
    if angle == 72: return "QUINTILE"
    if angle == 78: return "SETENTAYOCHO"
    if angle == 84: return "OCHENTAYCUATRO"
    if angle == 96: return "NOVENTAYSEIS"
    if angle == 102: return "CIENTODOS"
    if angle == 108: return "CIENTOOCHO"
    if angle == 114: return "CIENTOCATORCE"
    if angle == 126: return "CIENTOVEINTISEIS"
    if angle == 132: return "CIENTOTREINTAYDOS"
    if angle == 138: return "CIENTOTREINTAYOCHO"
    if angle == 144: return "CIENTOCUARENTAYCUATRO"
    if angle == 150: return "QUINCUNX"
    if angle == 156: return "CIENTOCINCUENTAYSEIS"
    if angle == 162: return "CIENTOSESENTAYDOS"
    if angle == 168: return "CIENTOSESENTAYOCHO"
    if angle == 174: return "CIENTOSETENTAYCUATRO"
    
    # Si no coincide con ninguno, usar un valor genérico
    return f"ANGLE_{angle}"

def get_aspect_color(aspect_type):
    """Obtiene el color para un tipo de aspecto"""
    if aspect_type == "Armónico Relevante":
        return "#000080"  # Azul oscuro
    elif aspect_type == "Inarmónico Relevante":
        return "#FF0000"  # Rojo
    elif aspect_type == "Armónico":
        return "#ADD8E6"  # Azul claro
    elif aspect_type == "Inarmónico":
        return "#ffff00"  # Amarillo
    else:
        return "#888888"  # Gris por defecto

@app.route('/')
def home():
    return send_file('index.html')

@app.route('/cities', methods=['GET'])
def get_cities():
    ciudad = request.args.get("ciudad")
    if not ciudad:
        return jsonify({"error": "Debes proporcionar una ciudad"}), 400

    print(f"Búsqueda recibida para ciudad: {ciudad}")
    
    # API key de Geoapify
    api_key = API_KEY
    
    # Usar la API de Geoapify para autocompletado de ciudades
    url = f"https://api.geoapify.com/v1/geocode/autocomplete?text={ciudad}&apiKey={api_key}&limit=20"
    
    try:
        # Hacer la petición a la API
        response = requests.get(url, timeout=10)
        print(f"Estado de respuesta Geoapify: {response.status_code}")
        
        if response.status_code != 200:
            print(f"Error en la API: {response.text}")
            raise Exception(f"Error en la API: {response.status_code}")
            
        data = response.json()
        
        # Crear lista de ciudades encontradas
        ciudades = []
        
        # Verificar si hay resultados
        if "features" in data and len(data["features"]) > 0:
            print(f"Número de resultados: {len(data['features'])}")
            
            for feature in data["features"]:
                props = feature["properties"]
                # Formatear el nombre de la ciudad con país
                nombre_ciudad = props.get("formatted", "")
                if nombre_ciudad:
                    print(f"Ciudad encontrada: {nombre_ciudad}")
                    ciudades.append(nombre_ciudad)
        else:
            print("No se encontraron resultados en la API")
        
        # Si no hay resultados, generar algunas opciones
        if not ciudades:
            print("Generando opciones")
            ciudades = [
                f"{ciudad}, España",
                f"{ciudad}, México",
                f"{ciudad}, Argentina",
                f"{ciudad}, Estados Unidos",
                f"{ciudad}, Colombia"
            ]
        
        print(f"Total ciudades a devolver: {len(ciudades)}")
        
        return jsonify({"ciudades": ciudades})
        
    except Exception as e:
        print(f"Error en búsqueda de ciudades: {str(e)}")
        # En caso de error, generar algunas opciones
        ciudades = [
            f"{ciudad}, España",
            f"{ciudad}, México",
            f"{ciudad}, Argentina",
            f"{ciudad}, Estados Unidos",
            f"{ciudad}, Colombia"
        ]
        
        return jsonify({"ciudades": ciudades})

@app.route('/calculate', methods=['POST', 'OPTIONS'])
def calculate():
    if request.method == 'OPTIONS':
        # Responder a la solicitud preflight de CORS
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST')
        return response
        
    try:
        data = request.get_json()
        if not data or not data.get('city'):
            return jsonify({"error": "Ciudad no especificada"}), 400
            
        city_data = obtener_datos_ciudad(data['city'], data['date'], data['time'])
        
        if isinstance(city_data, dict) and "error" in city_data:
            return jsonify(city_data), 400
            
        if isinstance(city_data, list) and len(city_data) > 0:
            city_data = city_data[0]
        else:
            return jsonify({"error": "No se pudo obtener información de la ciudad"}), 400
        
        try:
            # Obtener sistema zodiacal (tropical o sideral)
            use_sidereal = data.get('useSidereal', False)
            
            # Obtener zona horaria para las coordenadas
            timezone_info = obtener_zona_horaria(city_data, data['date'])
            
            # Convertir fecha y hora local a UTC
            utc_datetime = convertir_a_utc(data['date'], data['time'], timezone_info)
            
            # Calcular posiciones con el datetime UTC
            positions = calculate_positions_with_utc(utc_datetime, city_data["lat"], city_data["lon"], use_sidereal)
            
            # Calcular aspectos entre posiciones
            aspects = calculate_positions_aspects(positions)
            
            # Añadir número de casa a cada posición planetaria
            asc_pos = next((p for p in positions if p["name"] == "ASC"), None)
            if asc_pos:
                for planet in positions:
                    if planet["name"] not in ["ASC", "MC", "DSC", "IC", "PARTE_FORTUNA", "PARTE_ESPIRITU"]:
                        planet["house"] = get_house_number(planet["longitude"], asc_pos["longitude"])
            
            # Determinar si el nacimiento es seco (diurno) o húmedo (nocturno)
            sol_pos = next((p for p in positions if p["name"] == "SOL"), None)
            isDry = None
            if asc_pos and sol_pos:
                isDry = is_dry_birth(sol_pos["longitude"], asc_pos["longitude"])
            
            # Generar interpretaciones si el intérprete está disponible
            interpretations = {
                "planets_in_signs": [],
                "planets_in_houses": [],
                "aspects": [],
                "house_rulers": []
            }
            
            if interpreter and interpreter.root:
                # Interpretar planetas en signos
                for planet in positions:
                    if planet["name"] in ["SOL", "LUNA", "MERCURIO", "VENUS", "MARTE", "JÚPITER", "SATURNO"]:
                        sign_interp = interpreter.get_planet_in_sign(planet["name"], planet["sign"])
                        if sign_interp:
                            interpretations["planets_in_signs"].append({
                                "planet": planet["name"],
                                "sign": planet["sign"],
                                "interpretation": sign_interp
                            })
                
                # Interpretar planetas en casas
                if asc_pos:
                    for planet in positions:
                        if planet["name"] in ["SOL", "LUNA", "MERCURIO", "VENUS", "MARTE", "JÚPITER", "SATURNO"]:
                            house_num = get_house_number(planet["longitude"], asc_pos["longitude"])
                            house_interp = interpreter.get_planet_in_house(planet["name"], house_num)
                            if house_interp:
                                interpretations["planets_in_houses"].append({
                                    "planet": planet["name"],
                                    "house": house_num,
                                    "interpretation": house_interp
                                })
                
                # Interpretar aspectos
                for aspect in aspects:
                    # Obtener el tipo de aspecto
                    aspect_type = None
                    if aspect["type"] in ["CONJUNCTION", "SEXTILE", "TRINE", "OPPOSITION"]:
                        aspect_type = "Armónico Relevante"
                    elif aspect["type"] in ["SQUARE", "QUINCUNX"]:
                        aspect_type = "Inarmónico Relevante"
                    
                    if aspect_type and aspect["planet1"] in ["SOL", "LUNA", "MERCURIO", "VENUS", "MARTE", "JÚPITER", "SATURNO"] and \
                       aspect["planet2"] in ["SOL", "LUNA", "MERCURIO", "VENUS", "MARTE", "JÚPITER", "SATURNO"]:
                        
                        aspect_interp = interpreter.get_aspect_interpretation(aspect["planet1"], aspect["planet2"], aspect_type)
                        if aspect_interp:
                            interpretations["aspects"].append({
                                "planets": f"{aspect['planet1']} - {aspect['planet2']}",
                                "type": aspect_type,
                                "interpretation": aspect_interp
                            })
            
            # Calcular Ayanamsa para incluirlo en la respuesta
            ayanamsa = calculate_fagan_allen_ayanamsa(utc_datetime) if use_sidereal else 0
            
            # Construir respuesta
            response = {
                "positions": positions,
                "coordinates": {
                    "latitude": city_data["lat"],
                    "longitude": city_data["lon"]
                },
                "city": city_data["nombre"],
                "timezone": timezone_info,
                "local_time": f"{data['date']} {data['time']}",
                "utc_time": utc_datetime.strftime("%Y-%m-%d %H:%M"),
                "aspects": aspects,
                "interpretations": interpretations,
                "isDry": isDry,
                "zodiacSystem": "sidereal" if use_sidereal else "tropical",
                "ayanamsa": ayanamsa if use_sidereal else None
            }
            
            return jsonify(response)
            
        except Exception as timezone_error:
            print(f"Error con zona horaria: {str(timezone_error)}")
            import traceback
            traceback.print_exc()
            # Si hay error con la zona horaria, usamos valores básicos
            return jsonify({
                "error": f"Error en cálculos astrológicos: {str(timezone_error)}"
            }), 500
        
    except Exception as e:
        print(f"Error general: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("\nIniciando servidor de carta astral...")
    preload_resources()
    print("Recursos precargados. Iniciando servidor.")
    # Obtener puerto del entorno o usar 10002 por defecto
    port = int(os.environ.get("PORT", 10006))
    app.run(host='0.0.0.0', port=port, debug=False)
    calculate_positions_with_utc  