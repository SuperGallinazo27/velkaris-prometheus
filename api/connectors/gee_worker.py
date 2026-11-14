"""Earth Engine computations in an isolated, cancellable subprocess.

Only reduced dictionaries return to Render. No raster export/download methods.
"""
import json
import sys

DEM_ID = "NASA/NASADEM_HGT/001"
THERMAL_ID = "MODIS/061/MOD11A2"
METHOD = "prometheus_topography_plane_v1"
SCALE_M = 30
MAX_PIXELS = 40_000


def build_query(ee, sites, window_m, start_date=None, end_date=None):
    """Build a server-side graph; evaluation happens only once in main()."""
    source = ee.Image(DEM_ID)
    valid = source.select("swb").eq(0).And(source.select("num").gt(0)).And(source.select("num").lt(255))
    elevation = source.select("elevation").updateMask(valid)
    thermal = None
    if start_date:
        thermal = ee.ImageCollection(THERMAL_ID).filterDate(start_date, end_date).limit(8)

    def mask_thermal(image):
        raw = image.select("LST_Day_1km")
        # All four QA bit pairs zero: good mandatory/data QA and strict errors.
        mask = image.select("QC_Day").eq(0).And(raw.gte(7500)).And(raw.lte(65535))
        return raw.updateMask(mask).multiply(0.02).rename("lst")

    features = []
    for site in sites:
        zone = min(60, int((site["lon"] + 180) // 6) + 1)
        crs = f"EPSG:{(32600 if site['lat'] >= 0 else 32700) + zone}"
        projection = ee.Projection(crs).atScale(SCALE_M)
        point = ee.Geometry.Point([site["lon"], site["lat"]])
        center = point.transform(ee.Projection(crs), 1).coordinates()
        x, y = ee.Number(center.get(0)), ee.Number(center.get(1))
        half = window_m / 2
        # Coordinates in projection units are meters, not the pixel grid indices.
        area = ee.Geometry.Rectangle([x.subtract(half), y.subtract(half), x.add(half), y.add(half)],
                                     proj=crs, geodesic=False)
        options = dict(geometry=area, crs=crs, scale=SCALE_M, maxPixels=MAX_PIXELS, bestEffort=False)
        dem = elevation.resample("bilinear").reproject(crs=crs, scale=SCALE_M).rename("elevation")
        slope = ee.Terrain.slope(dem).rename("slope")
        reducers = ee.Reducer.minMax().combine(ee.Reducer.mean(), sharedInputs=True).combine(ee.Reducer.count(), sharedInputs=True)
        stats = dem.addBands(slope).reduceRegion(reducers, **options)
        all_pixels = ee.Image.constant(1).rename("all").reduceRegion(ee.Reducer.count(), **options).get("all")
        xy = ee.Image.pixelCoordinates(projection).toFloat()
        means = xy.reduceRegion(ee.Reducer.mean(), **options)
        centered = xy.subtract(ee.Image.constant([means.get("x"), means.get("y")]).rename(["x", "y"]))
        predictors = ee.Image.constant(1).rename("constant").addBands(centered).updateMask(dem.mask())
        fit = predictors.addBands(dem).reduceRegion(ee.Reducer.linearRegression(3, 1), **options)
        residuals = fit.get("residuals")
        rms = ee.Algorithms.If(residuals, ee.Array(residuals).get([0]), None)
        properties = {"id": site["id"], "crs": crs, "stats": stats, "total_pixels": all_pixels,
                      "roughness_m": rms, "thermal": None}
        if thermal is not None:
            images = thermal.filterBounds(area)
            masked_empty = ee.Image.constant(0).rename("lst").updateMask(ee.Image.constant(0))
            composite = images.map(mask_thermal).merge(ee.ImageCollection([masked_empty])).mean()
            temperature = composite.reduceRegion(
                ee.Reducer.mean().combine(ee.Reducer.count(), sharedInputs=True),
                geometry=area, crs="SR-ORG:6974", scale=1000, maxPixels=100, bestEffort=False,
            )
            properties["thermal"] = {"stats": temperature, "images": images.size(),
                                     "first_ms": images.aggregate_min("system:time_start"),
                                     "last_ms": images.aggregate_max("system:time_start")}
        features.append(ee.Feature(None, properties))
    return ee.FeatureCollection(features)


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(32_769))
        import ee
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            payload["credentials_path"], scopes=["https://www.googleapis.com/auth/earthengine"])
        # These SDK globals are isolated per worker, never shared between requests.
        ee.data.setDeadline(10_000)
        ee.data.setMaxRetries(0)
        ee.Initialize(credentials=credentials, project=payload["project_id"])
        query = payload["query"]
        result = build_query(ee, query["sites"], query["window_m"], query.get("start_date"), query.get("end_date")).getInfo()
        encoded = json.dumps(result, allow_nan=False).encode()
        if len(encoded) > 65_536:
            raise ValueError("Reduced response exceeded budget")
        sys.stdout.buffer.write(encoded)
    except Exception:
        # Credentials, provider errors and paths never leave this process.
        sys.stdout.write('{"error":"gee_unavailable"}')
        sys.exit(1)


if __name__ == "__main__":
    main()
