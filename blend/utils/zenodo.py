import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)

def write_public(forecast, messages, output_path, date):
    """
    Write the public forecast to a CSV file.

    Parameters
    ----------
    forecast : pd.DataFrame
        The forecast DataFrame.
    messages : list
        List of messages to include in the CSV file.
    output_dir : str
        The output directory.
    date : str
        The date string.

    Returns
    -------
    None
    """
    if date.weekday() in [1, 3] and date.hour == 0: 
        output_dir = output_path / "zenodo"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
        date_str = date.strftime("%d_%B_%Y")

        forecast_name = f"forecast_{date_str}.csv"
        forecast_path = output_dir / forecast_name
        forecast.to_csv(forecast_path, index=False)
        logging.info(f"Wrote {forecast_name} to {output_dir}")

        messages_name = f"messages_{date_str}.csv"
        messages_path = output_dir / messages_name
        messages.to_csv(messages_path, index=False, encoding='utf-8-sig')
        logging.info(f"Wrote {messages_name} to {output_dir}")

    else:
        logging.info(
            f"Not writing forecast to Zenodo: {date.strftime("%A, %B %d %Y %H:%M")} is not a Tuesday or Thursday at 00:00"
        )
