// Host-side parity test: feeds the samples in ml/build/parity_input.csv through
// the firmware's bmsml::Engine and compares every output with what the Python
// reference (ml/bms_reference.py) produced in parity_expected.csv.
//
// Build and run with tests/run_parity_test.py, or by hand:
//   g++ -O2 -std=c++17 -I code/smart_bms_ml ml/tests/parity_test.cpp -o parity_test
//   ./parity_test ml/build/parity_input.csv ml/build/parity_expected.csv

#include <math.h>
#include <stdio.h>

#include "bms_ml.h"

struct Tolerance
{
    const char *name;
    float tol;
    bool relative; // tolerance scales with |expected| above 1 (for unbounded values)
    double maxErr;
    long fails;
};

static void check(Tolerance &t, double got, double want)
{
    double err = fabs(got - want);
    if (t.relative && fabs(want) > 1)
        err /= fabs(want);
    if (err > t.maxErr)
        t.maxErr = err;
    if (err > t.tol)
        t.fails++;
}

int main(int argc, char **argv)
{
    if (argc != 3)
    {
        fprintf(stderr, "usage: %s parity_input.csv parity_expected.csv\n", argv[0]);
        return 2;
    }
    FILE *in = fopen(argv[1], "r");
    FILE *exp = fopen(argv[2], "r");
    if (!in || !exp)
    {
        fprintf(stderr, "cannot open input files\n");
        return 2;
    }
    char line[512];
    fgets(line, sizeof line, in); // headers
    fgets(line, sizeof line, exp);

    Tolerance tCur = {"currentAvg_A", 1e-5f, false, 0, 0};
    Tolerance tDv = {"dVdt_mVmin", 2e-3f, true, 0, 0};
    Tolerance tDt = {"dTdt_Cmin", 2e-4f, true, 0, 0};
    Tolerance tMl = {"soc_ml", 0.02f, false, 0, 0};
    Tolerance tScore = {"anomaly_score", 1e-3f, true, 0, 0};
    Tolerance tV1 = {"soc_v1", 0.02f, false, 0, 0};
    Tolerance tFused = {"soc_fused", 0.05f, false, 0, 0};
    long readyMismatch = 0, anomalyMismatch = 0, samples = 0, settledSamples = 0, anomalySamples = 0;

    bmsml::Engine engine;
    int prevRun = -1;
    long prevT = 0;
    int run, load;
    long tMs;
    float v, i, t;
    while (fscanf(in, "%d,%ld,%f,%f,%f,%d", &run, &tMs, &v, &i, &t, &load) == 6)
    {
        int eRun, eReady, eAnomaly;
        double eCur, eDv, eDt, eMl, eScore, eV1, eFused;
        if (fscanf(exp, "%d,%d,%lf,%lf,%lf,%lf,%lf,%d,%lf,%lf", &eRun, &eReady, &eCur, &eDv, &eDt,
                   &eMl, &eScore, &eAnomaly, &eV1, &eFused) != 10 ||
            eRun != run)
        {
            fprintf(stderr, "expected file out of step at sample %ld\n", samples);
            return 2;
        }

        // Same sequence of calls the sketch makes for each valid sample.
        float dt;
        if (run != prevRun)
        {
            engine.reset();
            dt = bmsml::SAMPLE_PERIOD_MS / 1000.0f;
        }
        else
        {
            dt = (tMs - prevT) / 1000.0f;
        }
        prevRun = run;
        prevT = tMs;
        const bmsml::Output &o = engine.update(v, i, t, load != 0, dt);

        samples++;
        if (o.mlReady != (eReady != 0))
            readyMismatch++;
        if (o.anomaly != (eAnomaly != 0))
            anomalyMismatch++;
        anomalySamples += o.anomaly;
        check(tV1, o.socV1, eV1);
        check(tFused, o.socFused, eFused);
        if (o.mlReady && eReady)
        {
            settledSamples++;
            check(tCur, o.features.currentAvg_A, eCur);
            check(tDv, o.features.dVdt_mVmin, eDv);
            check(tDt, o.features.dTdt_Cmin, eDt);
            check(tMl, o.socMl, eMl);
            check(tScore, o.anomalyScore, eScore);
        }
    }

    printf("samples %ld (settled %ld, flagged anomalous %ld)\n", samples, settledSamples, anomalySamples);
    printf("ml_ready mismatches: %ld\n", readyMismatch);
    printf("anomaly flag mismatches: %ld\n", anomalyMismatch);
    bool ok = samples > 0 && readyMismatch == 0 && anomalyMismatch == 0;
    Tolerance *all[] = {&tCur, &tDv, &tDt, &tMl, &tScore, &tV1, &tFused};
    for (Tolerance *t : all)
    {
        printf("%-14s max |C++ - Python| = %.3g (tolerance %.3g, %ld over)\n", t->name, t->maxErr, t->tol, t->fails);
        ok = ok && t->fails == 0;
    }
    printf(ok ? "PARITY OK\n" : "PARITY FAILED\n");
    return ok ? 0 : 1;
}
