# Extract counts of each Köppen-Geiger class for each country, exported to CSV.
import argparse
import math
import os.path
import pdb
import signal
import sys
import tempfile

import osgeo.gdal
import osgeo.gdal_array
import osgeo.ogr
import numpy as np
import pandas as pd

import admin_names


pd.set_option("display.max_rows", 500)
pd.set_option("display.max_columns", 40)
pd.options.display.float_format = '{:.2f}'.format
osgeo.gdal.PushErrorHandler("CPLQuietErrorHandler")
np.set_printoptions(threshold=sys.maxsize)



class KGlookup:
    """Lookup table of pixel color to Köppen-Geiger class.

       Mappings come from legend.txt file in ZIP archive from
       https://www.nature.com/articles/sdata2018214.pdf at http://www.gloh2o.org/koppen/
    """
    kg_colors = {
        (  0,   0, 255): 'Af',  (  0, 120, 255): 'Am',  ( 70, 170, 250): 'Aw',
        (255,   0,   0): 'BWh', (255, 150, 150): 'BWk', (245, 165,   0): 'BSh',
        (255, 220, 100): 'BSk',
        (255, 255,   0): 'Csa', (200, 200,   0): 'Csb', (150, 150,   0): 'Csc',
        (150, 255, 150): 'Cwa', (100, 200, 100): 'Cwb', ( 50, 150,  50): 'Cwc',
        (200, 255,  80): 'Cfa', (100, 255,  80): 'Cfb', ( 50, 200,   0): 'Cfc',
        (255,   0, 255): 'Dsa', (200,   0, 200): 'Dsb', (150,  50, 150): 'Dsc',
        (150, 100, 150): 'Dsd', (170, 175, 255): 'Dwa', ( 90, 120, 220): 'Dwb',
        ( 75,  80, 180): 'Dwc', ( 50,   0, 135): 'Dwd', (  0, 255, 255): 'Dfa',
        ( 55, 200, 255): 'Dfb', (  0, 125, 125): 'Dfc', (  0,  70,  95): 'Dfd',
        (178, 178, 178): 'ET',  (102, 102, 102): 'EF',
        }

    warpOptions = ['CUTLINE_ALL_TOUCHED=TRUE']

    def __init__(self, ctable):
        self.ctable = ctable

    def get_index(self, label):
        if int(label) < 0:
            return None
        r, g, b, a = self.ctable.GetColorEntry(int(label))
        color = (r, g, b)
        if color == (255, 255, 255) or color == (0, 0, 0):
            # blank pixel == masked off, just skip it.
            return None
        return self.kg_colors[color]

    def get_columns(self):
        return self.kg_colors.values()


class ESA_LC_lookup:
    """Pixel color to Land Cover class in ESACCI-LC-L4-LCCS-Map-300m-P1Y-2015-v2.0.7.tif

       There are legends of LCCS<->color swatch in both
       http://maps.elie.ucl.ac.be/CCI/viewer/download/ESACCI-LC-QuickUserGuide-LC-Maps_v2-0-7.pdf
       and section 9.1 of http://maps.elie.ucl.ac.be/CCI/viewer/download/ESACCI-LC-Ph2-PUGv2_2.0.pdf
       but these were generated from Microsoft Word documents and contain embedded color profiles
       for color correction, meaning that what is displayed on the screen (and to any sort of
       Digital Color Meter tool) have been shifted and do not match the original colors. Do not
       attempt to use the color legend in these files.

       There is a table of RGB to LCCS values in
       http://maps.elie.ucl.ac.be/CCI/viewer/download/ESACCI-LC-Legend.csv
       however: the GeoTIFF file as shipped is greyscale with no color table. Each pixel is 8 bits
       not because it is looked up in a color table, it is 8 bits because it has been converted to
       greyscale. The authors appear to have carefully chosen the RGB values such that when
       converted to greyscale, LCCS class 10 will have a grey value of 10, LCCS class 11 will have
       a grey value of 11, and so on.

       So we don't need a lookup table, greyscale absolute values directly equal the LCCS class."""

    warpOptions = ['CUTLINE_ALL_TOUCHED=TRUE']

    def get_index(self, label):
        if label == 0:
            # black == no land cover (like water), just skip it.
            return None
        return label

    def get_columns(self):
        """Return list of LCCS classes present in this dataset."""
        return [10, 11, 12, 20, 30, 40, 50, 60, 61, 62, 70, 71, 72, 80, 81, 82, 90, 100, 110,
                120, 121, 122, 130, 140, 150, 151, 152, 153, 160, 170, 180, 190, 200, 201, 202,
                210, 220]



class FAO_LC_lookup:
    """Pixel color to Land Cover class in 
    """

    warpOptions = ['CUTLINE_ALL_TOUCHED=TRUE']
    land_covers = {1: 'Artificial Surfaces', 2: 'Cropland', 3: 'Grassland',
            4: 'Tree Covered Areas', 5: 'Shrubs Covered Areas',
            6: 'Herbaceous vegetation, aquatic or regularly flooded',
            7: 'Mangroves', 8: 'Sparse vegetation', 9: 'Baresoil', 10: 'Snow and glaciers',
            11: 'Waterbodies'}

    def get_index(self, label):
        if label == 0 or label == 255:
            # black == no land cover (like water), just skip it.
            return None
        return self.land_covers[label]

    def get_columns(self):
        """Return list of LCCS classes present in this dataset."""
        return self.land_covers.values()


class SlopeLookup:
    """Geomorpho90m slope file dtm_slope_merit.dem_m_250m_s0..0cm_2018_v1.0.tif
       has been pre-processed with classify_geomorpho90m_slope.py to classify slope values
       into the buckets defined in GAEZ 3.0.
    """

    warpOptions = ['CUTLINE_ALL_TOUCHED=TRUE']
    gaez_slopes = ["0-0.5%", "0.5-2%", "2-5%", "5-8%", "8-16%", "16-30%", "30-45%", ">45%"]

    def get_index(self, label):
        if label == 255:
            return None
        return self.gaez_slopes[label]

    def get_columns(self):
        """Return list of GAEZ slope classes."""
        return self.gaez_slopes


class WorkabilityLookup:
    """Workability TIF has been pre-processed, pixel values are workability class.
    """

    warpOptions = None
    def get_index(self, label):
        if label == 0:
            return None
        return label

    def get_columns(self):
        return range(1, 8)


def start_pdb(sig, frame):
    """Start PDB on a signal."""
    pdb.Pdb().set_trace(frame)


def one_feature_shapefile(mapfilename, a3, idx, feature, tmpdir, srs, warpOptions):
    """Make a new shapefile, to hold the one Feature we're looking at."""
    driver = osgeo.ogr.GetDriverByName("ESRI Shapefile")
    shpfile = os.path.join(tmpdir, f'{a3}_{idx}_feature_mask.shp')
    data_source = driver.CreateDataSource(shpfile)
    layer = data_source.CreateLayer("feature", geom_type=osgeo.ogr.wkbPolygon, srs=srs)
    new_feat = osgeo.ogr.Feature(layer.GetLayerDefn())
    geom = feature.GetGeometryRef()
    new_feat.SetGeometry(geom)
    layer.CreateFeature(new_feat)

    # Close datasets. GDAL needs this as it implements some of the work in the destructor.
    new_feat = None
    data_source = None
    layer = None

    # Apply shapefile as a mask, and crop to the size of the mask
    clippedfile = os.path.join(tmpdir, f'{a3}_{idx}_feature.tif')
    result = osgeo.gdal.Warp(clippedfile, mapfilename, cutlineDSName=shpfile, cropToCutline=True,
            warpOptions=warpOptions)
    if result is not None:
        return clippedfile


def update_df_from_image(filename, admin, lookupobj, df):
    """Count classes by pixel, add to df."""
    img = osgeo.gdal.Open(filename, osgeo.gdal.GA_ReadOnly)
    xmin, xsiz, xrot, ymin, yrot, ysiz = img.GetGeoTransform()
    band = img.GetRasterBand(1)
    yrad = math.radians(abs(ysiz))
    y = math.radians(ymin)
    for yoff in range(0, int(band.YSize)):
        row = band.ReadAsArray(0, yoff, int(band.XSize), 1)
        y1 = y - (yrad / 2)
        # https://en.wikipedia.org/wiki/Longitude#Length_of_a_degree_of_longitude
        xlen = abs(xsiz) * (math.cos(y1) * math.pi * 6378.137 /
                (180 * math.sqrt(1 - 0.00669437999014 * (math.sin(y1) ** 2))))
        # https://en.wikipedia.org/wiki/Latitude#Length_of_a_degree_of_latitude
        ylen = abs(ysiz) * (111.132954 - (0.559822 * math.cos(2 * y1)) +
                (0.001175 * math.cos(4 * y1)))
        km2 = xlen * ylen
        labels, counts = np.unique(row[0], return_counts=True)
        for i in range(len(labels)):
            idx = lookupobj.get_index(labels[i])
            if idx is None:
                continue
            df.loc[admin, idx] += (counts[i] * km2)
        y -= yrad


def process_map(shapefilename, mapfilename, lookupobj, csvfilename):
    tmpdirobj = tempfile.TemporaryDirectory()
    df = pd.DataFrame(columns=lookupobj.get_columns(), dtype=float)
    df.index.name = 'Country'
    shapefile = osgeo.ogr.Open(shapefilename)
    assert shapefile.GetLayerCount() == 1
    layer = shapefile.GetLayerByIndex(0)
    srs = layer.GetSpatialRef()

    for idx, feature in enumerate(layer):
        admin = admin_names.lookup(feature.GetField("ADMIN"))
        if admin is None:
            continue
        a3 = feature.GetField("SOV_A3")
        if admin not in df.index:
            df.loc[admin] = [0] * len(df.columns)

        clippedfile = one_feature_shapefile(mapfilename=mapfilename, a3=a3, idx=idx,
                feature=feature, tmpdir=tmpdirobj.name, srs=srs,
                warpOptions=lookupobj.warpOptions)
        if clippedfile:
            print(f"Processing {admin:<41} #{a3}_{idx}")
            update_df_from_image(filename=clippedfile, admin=admin, lookupobj=lookupobj, df=df)
        else:
            print(f"Skipping empty {admin:<41} #{a3}_{idx}")

    outputfilename = os.path.join('results', csvfilename)
    df.sort_index(axis='index').to_csv(outputfilename, float_format='%.2f')


def km2_block(nrows, ncols, y_off, img):
    """Return (nrows,ncols) numpy array of pixel area in sq km."""
    x_mindeg, x_sizdeg, x_rot, y_mindeg, y_rotdeg, y_sizdeg = img.GetGeoTransform()
    yrad = math.radians(abs(y_sizdeg))
    km2 = np.empty((nrows, ncols))
    y = math.radians(y_mindeg + (y_off * y_sizdeg)) - (yrad / 2)
    for i in range(nrows):
        # https://en.wikipedia.org/wiki/Longitude#Length_of_a_degree_of_longitude
        xlen = abs(x_sizdeg) * (math.cos(y) * math.pi * 6378.137 /
                (180 * math.sqrt(1 - 0.00669437999014 * (math.sin(y) ** 2))))
        # https://en.wikipedia.org/wiki/Latitude#Length_of_a_degree_of_latitude
        ylen = abs(y_sizdeg) * (111.132954 - (0.559822 * math.cos(2 * y)) +
                (0.001175 * math.cos(4 * y)))
        km2[i, :] = xlen * ylen
        y -= yrad
    return km2


def is_sparse(band, x, y, ncols, nrows):
    """Return True if the given coordinates are a sparse hole in the image."""
    (flags, pct) = band.GetDataCoverageStatus(x, y, ncols, nrows)
    if flags == osgeo.gdal.GDAL_DATA_COVERAGE_STATUS_EMPTY and pct == 0.0:
        return True


def blklim(coord, blksiz, totsiz):
    if (coord + blksiz) < totsiz:
        return blksiz
    else:
        return totsiz - coord


def process_map_with_masks(mapfilename, lookupobj, csvfilename):
    shapefilename = 'data/ne_10m_admin_0_countries/ne_10m_admin_0_countries.shp'
    tmpdirobj = tempfile.TemporaryDirectory()
    df = pd.DataFrame(columns=lookupobj.get_columns(), dtype=float)
    df.index.name = 'Country'
    img = osgeo.gdal.Open(mapfilename, osgeo.gdal.GA_ReadOnly)
    imgband = img.GetRasterBand(1)
    shapefile = osgeo.ogr.Open(shapefilename)
    assert shapefile.GetLayerCount() == 1
    layer = shapefile.GetLayerByIndex(0)

    for idx, feature in enumerate(layer):
        admin = admin_names.lookup(feature.GetField("ADMIN"))
        if admin is None:
            continue
        a3 = feature.GetField("SOV_A3")
        if admin not in df.index:
            df.loc[admin] = [0] * len(df.columns)

        print(f"Processing {admin:<41} #{a3}_{idx}")
        maskfilename = f"masks/{a3}_{idx}_1km_mask._tif"
        maskimg = osgeo.gdal.Open(maskfilename, osgeo.gdal.GA_ReadOnly)
        maskband = maskimg.GetRasterBand(1)
        x_siz = maskband.XSize
        y_siz = maskband.YSize
        x_blksiz, y_blksiz = maskband.GetBlockSize()
        for y in range(0, y_siz, y_blksiz):
            nrows = blklim(coord=y, blksiz=y_blksiz, totsiz=y_siz)
            for x in range(0, x_siz, x_blksiz):
                ncols = blklim(coord=x, blksiz=x_blksiz, totsiz=x_siz)
                if is_sparse(band=maskband, x=x, y=y, ncols=ncols, nrows=nrows):
                    # sparse hole in image, no data to process
                    continue

                block = imgband.ReadAsArray(x, y, ncols, nrows)
                mask = maskband.ReadAsArray(x, y, ncols, nrows)
                km2 = km2_block(nrows=nrows, ncols=ncols, y_off=y, img=maskimg)
                masked = np.ma.masked_array(block, mask=np.logical_not(mask)).filled(-1)
                for label in np.unique(masked):
                    typ = lookupobj.get_index(label)
                    if typ is None:
                        continue
                    df.loc[admin, typ] += km2[masked == label].sum()
    outputfilename = os.path.join('results', csvfilename)
    df.sort_index(axis='index').to_csv(outputfilename, float_format='%.2f')


if __name__ == '__main__':
    signal.signal(signal.SIGUSR1, start_pdb)
    os.environ['GDAL_CACHEMAX'] = '128'

    parser = argparse.ArgumentParser(description='Videos to images')
    parser.add_argument('--lc', default=False, required=False,
                        action='store_true', help='process land cover')
    parser.add_argument('--kg', default=False, required=False,
                        action='store_true', help='process Köppen-Geiger')
    parser.add_argument('--sl', default=False, required=False,
                        action='store_true', help='process slope')
    parser.add_argument('--wk', default=False, required=False,
                        action='store_true', help='process workability')
    parser.add_argument('--all', default=False, required=False,
                        action='store_true', help='process all')
    args = parser.parse_args()

    processed = False
    shapefilename = 'data/ne_10m_admin_0_countries/ne_10m_admin_0_countries.shp'

    if args.lc or args.all:
        mapfilename = 'data/FAO/glc_shv10_dominant_landcover.tif'
        csvfilename = 'FAO-Land-Cover-by-country.csv'
        print(mapfilename)
        lookupobj = FAO_LC_lookup()
        process_map_with_masks(mapfilename=mapfilename, lookupobj=lookupobj, csvfilename=csvfilename)
        print('\n')
        sys.exit(0)

        land_cover_files = [
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2015-v2.0.7.tif', 'Land-Cover-by-country.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2014-v2.0.7.tif', 'Land-Cover-by-country-2014.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2013-v2.0.7.tif', 'Land-Cover-by-country-2013.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2012-v2.0.7.tif', 'Land-Cover-by-country-2012.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2011-v2.0.7.tif', 'Land-Cover-by-country-2011.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2010-v2.0.7.tif', 'Land-Cover-by-country-2010.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2009-v2.0.7.tif', 'Land-Cover-by-country-2009.csv'),
                ('data/ucl_elie/ESACCI-LC-L4-LCCS-Map-300m-P1Y-2008-v2.0.7.tif', 'Land-Cover-by-country-2008.csv')
                ]
        for (mapfilename, csvfilename) in land_cover_files:
            if not os.path.exists(mapfilename):
                print(f"Skipping missing {mapfilename}")
                continue
            print(mapfilename)
            lookupobj = ESA_LC_lookup()
            process_map(shapefilename=shapefilename, mapfilename=mapfilename, lookupobj=lookupobj,
                        csvfilename=csvfilename)
            print('\n')
        processed = True

    if args.kg or args.all:
        mapfilename = 'data/Beck_KG_V1/Beck_KG_V1_present_0p0083.tif'
        csvfilename = 'Köppen-Geiger-present-by-country.csv'
        print(mapfilename)
        img = osgeo.gdal.Open(mapfilename, osgeo.gdal.GA_ReadOnly)
        ctable = img.GetRasterBand(1).GetColorTable()
        lookupobj = KGlookup(ctable)
        process_map_with_masks(mapfilename=mapfilename, lookupobj=lookupobj, csvfilename=csvfilename)
        print('\n')

        mapfilename = 'data/Beck_KG_V1/Beck_KG_V1_future_0p0083.tif'
        csvfilename = 'Köppen-Geiger-future-by-country.csv'
        print(mapfilename)
        img = osgeo.gdal.Open(mapfilename, osgeo.gdal.GA_ReadOnly)
        ctable = img.GetRasterBand(1).GetColorTable()
        lookupobj = KGlookup(ctable)
        process_map_with_masks(mapfilename=mapfilename, lookupobj=lookupobj, csvfilename=csvfilename)
        print('\n')
        processed = True

    if args.sl or args.all:
        mapfilename = 'data/geomorpho90m/classified_slope_merit_dem_250m_s0..0cm_2018_v1.0.tif'
        csvfilename = 'Slope-by-country.csv'
        print(mapfilename)
        lookupobj = SlopeLookup()
        process_map(shapefilename=shapefilename, mapfilename=mapfilename, lookupobj=lookupobj,
                    csvfilename=csvfilename)
        print('\n')
        processed = True

    if args.wk or args.all:
        mapfilename = 'data/FAO/workability_FAO_sq7_10km.tif'
        csvfilename = 'Workability-by-country.csv'
        print(mapfilename)
        lookupobj = WorkabilityLookup()
        process_map(shapefilename=shapefilename, mapfilename=mapfilename, lookupobj=lookupobj,
                    csvfilename=csvfilename)
        print('\n')
        processed = True


    if not processed:
        print('Select one of:')
        print('\t-lc : Land Cover')
        print('\t-kg : Köppen-Geiger')
        print('\t-sl : Slope')
        print('\t-wk : Workability')
        print('\t-all')
